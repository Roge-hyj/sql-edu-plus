"""Explicit boundaries for gradually migrating legacy witness probes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .planner import (
    CellConstraint,
    ConstraintConflict,
    ConstraintLedger,
    write_owner,
)
from .schema_scope import ColumnRef

ProbeApply = Callable[
    [dict[str, list[dict[str, Any]]], dict[str, list[str]], str, str, list[Any]],
    None,
]
ProbeSqlTrigger = Callable[[str, str], bool]
ProbeConstraintFactory = Callable[[Iterable[Any]], Iterable[CellConstraint]]
ProbeColumnSetFactory = Callable[
    [dict[str, list[str]], str, str, list[Any]],
    Iterable[ColumnRef],
]


@dataclass(frozen=True)
class LegacyProbeAdapter:
    name: str
    phase: int
    apply: ProbeApply
    diff_types: frozenset[str] = frozenset()
    clauses: frozenset[str] = frozenset()
    knowledge_points: frozenset[str] = frozenset()
    obligation_ids: frozenset[str] = frozenset()
    read_set: frozenset[ColumnRef] = frozenset()
    write_set: frozenset[ColumnRef] = frozenset()
    read_set_factory: ProbeColumnSetFactory | None = None
    write_set_factory: ProbeColumnSetFactory | None = None
    cell_constraints: tuple[CellConstraint, ...] = ()
    constraint_factory: ProbeConstraintFactory | None = None
    sql_trigger: ProbeSqlTrigger | None = None
    activation_guard: ProbeSqlTrigger | None = None
    locked: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches(
        self,
        ast_diffs: Iterable[Any],
        obligation_ids: Iterable[str] = (),
        *,
        standard_sql: str = "",
        student_sql: str = "",
    ) -> bool:
        diffs = list(ast_diffs)
        requested = set(obligation_ids)
        if self.activation_guard and not self.activation_guard(standard_sql, student_sql):
            return False
        if requested and self.obligation_ids & requested:
            return True
        # SQL-shape triggers are a legacy fallback for callers that have no
        # AST/obligation information.  They must not fire inside an isolated
        # witness world: a window-only world containing an ``AND`` elsewhere
        # in the query must not activate the logical probe.
        if not diffs and not requested and self.sql_trigger and self.sql_trigger(standard_sql, student_sql):
            return True
        for diff in diffs:
            if self.diff_types and getattr(diff, "diff_type", None) not in self.diff_types:
                continue
            if self.clauses and getattr(diff, "clause_category", None) not in self.clauses:
                continue
            if self.knowledge_points and getattr(diff, "knowledge_point_id", None) not in self.knowledge_points:
                continue
            if self.diff_types or self.clauses or self.knowledge_points:
                return True
        return False

    def validate_write(self, target: ColumnRef) -> None:
        target_key = (target.relation.lower(), target.column.lower(), target.query_scope.lower())
        declared = {
            (item.relation.lower(), item.column.lower(), item.query_scope.lower())
            for item in self.write_set
        }
        if self.write_set and target_key not in declared:
            raise PermissionError(
                f"legacy probe {self.name!r} wrote outside declared write_set: {target_key}"
            )


@dataclass
class AdapterRun:
    adapter: str
    activated: bool
    applied: bool = False
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    constraint_conflicts: list[ConstraintConflict] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    declared_read_set: list[dict[str, str]] = field(default_factory=list)
    declared_write_set: list[dict[str, str]] = field(default_factory=list)
    write_set_satisfied: bool | None = None


class LegacyProbeRegistry:
    def __init__(self) -> None:
        self._items: list[LegacyProbeAdapter] = []

    def register(self, adapter: LegacyProbeAdapter) -> None:
        if any(item.name == adapter.name for item in self._items):
            raise ValueError(f"duplicate legacy probe adapter: {adapter.name}")
        self._items.append(adapter)

    def active(
        self,
        ast_diffs: Iterable[Any],
        obligation_ids: Iterable[str] = (),
        *,
        standard_sql: str = "",
        student_sql: str = "",
    ) -> list[LegacyProbeAdapter]:
        diffs, obligations = list(ast_diffs), list(obligation_ids)
        return sorted(
            (
                item
                for item in self._items
                if item.matches(
                    diffs,
                    obligations,
                    standard_sql=standard_sql,
                    student_sql=student_sql,
                )
            ),
            key=lambda item: (item.phase, item.name),
        )

    def __len__(self) -> int:
        return len(self._items)

    def get(self, name: str) -> LegacyProbeAdapter:
        for item in self._items:
            if item.name == name:
                return item
        raise KeyError(name)


def adapter_owner(adapter: LegacyProbeAdapter) -> str:
    return f"legacy:{adapter.name}"


def _snapshot_database(
    data: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    return {
        table: [dict(row) for row in rows]
        for table, rows in data.items()
    }


def _database_writes(
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    writes: list[dict[str, Any]] = []
    for table in sorted(set(before) | set(after)):
        before_rows = before.get(table, [])
        after_rows = after.get(table, [])
        if len(before_rows) != len(after_rows):
            writes.append({
                "table": table,
                "row_index": None,
                "column": "*",
                "before": len(before_rows),
                "after": len(after_rows),
                "kind": "row_topology_changed",
            })
        for index in range(min(len(before_rows), len(after_rows))):
            previous = before_rows[index]
            current = after_rows[index]
            for column in sorted(set(previous) | set(current)):
                if previous.get(column) == current.get(column) and (
                    column in previous
                ) == (column in current):
                    continue
                writes.append({
                    "table": table,
                    "row_index": index,
                    "column": column,
                    "before": previous.get(column),
                    "after": current.get(column),
                    "kind": "cell_changed",
                })
    return writes


def _restore_database(
    data: dict[str, list[dict[str, Any]]],
    before: dict[str, list[dict[str, Any]]],
) -> None:
    for table in list(data):
        if table not in before:
            del data[table]
    for table, previous_rows in before.items():
        current_rows = data.setdefault(table, [])
        while len(current_rows) < len(previous_rows):
            current_rows.append({})
        del current_rows[len(previous_rows):]
        for index, previous in enumerate(previous_rows):
            row = current_rows[index]
            # Explicit base-dict operations avoid turning rollback itself into
            # another TrackedRow write event.
            dict.clear(row)
            dict.update(row, previous)


def _validate_adapter_writes(
    adapter: LegacyProbeAdapter,
    writes: Iterable[dict[str, Any]],
    declared_write_set: Iterable[ColumnRef] | None = None,
) -> None:
    resolved_write_set = set(
        adapter.write_set if declared_write_set is None else declared_write_set
    )
    if not resolved_write_set:
        return
    declared = {
        (item.relation.lower(), item.column.lower())
        for item in resolved_write_set
    }
    for write in writes:
        target = (
            str(write.get("table") or "").lower(),
            str(write.get("column") or "").lower(),
        )
        if write.get("kind") != "cell_changed" or target not in declared:
            raise PermissionError(
                f"legacy probe {adapter.name!r} wrote outside declared write_set: {target}"
            )


def _constraint_conflict_evidence(
    adapter: LegacyProbeAdapter,
    conflict: ConstraintConflict,
) -> dict[str, Any]:
    return {
        "adapter": adapter.name,
        "type": "ConstraintConflict",
        "message": "declared cell constraint conflicts with the current world",
        "reason": conflict.reason,
        "target": list(conflict.target),
        "existing": dict(conflict.existing.__dict__),
        "incoming": dict(conflict.incoming.__dict__),
        "action": "split_world",
    }


def _column_set_evidence(columns: Iterable[ColumnRef]) -> list[dict[str, str]]:
    return [
        {
            "relation": item.relation,
            "column": item.column,
            "query_scope": item.query_scope,
        }
        for item in sorted(set(columns))
    ]


def run_adapter(
    adapter: LegacyProbeAdapter,
    *,
    data: dict[str, list[dict[str, Any]]],
    schema: dict[str, list[str]],
    standard_sql: str,
    student_sql: str,
    ast_diffs: list[Any],
    obligation_ids: Iterable[str] = (),
    obligations: Iterable[Any] = (),
    ledger: ConstraintLedger | None = None,
) -> AdapterRun:
    resolved_obligations = list(obligations)
    requested_obligation_ids = list(obligation_ids) or [
        str(getattr(item, "id", ""))
        for item in resolved_obligations
        if getattr(item, "id", "")
    ]
    if not adapter.matches(
        ast_diffs,
        requested_obligation_ids,
        standard_sql=standard_sql,
        student_sql=student_sql,
    ):
        return AdapterRun(adapter=adapter.name, activated=False)
    result = AdapterRun(adapter=adapter.name, activated=True)
    before = _snapshot_database(data)
    try:
        declared_read_set = set(adapter.read_set)
        declared_write_set = set(adapter.write_set)
        if adapter.read_set_factory is not None:
            declared_read_set.update(
                adapter.read_set_factory(
                    schema, standard_sql, student_sql, ast_diffs
                )
            )
        if adapter.write_set_factory is not None:
            declared_write_set.update(
                adapter.write_set_factory(
                    schema, standard_sql, student_sql, ast_diffs
                )
            )
        result.declared_read_set = _column_set_evidence(declared_read_set)
        result.declared_write_set = _column_set_evidence(declared_write_set)
        declared_constraints = list(adapter.cell_constraints)
        if adapter.constraint_factory is not None:
            declared_constraints.extend(adapter.constraint_factory(resolved_obligations))
        if ledger is not None and declared_constraints:
            conflict_offset = len(ledger.conflicts)
            if not ledger.add_many(declared_constraints):
                result.constraint_conflicts.extend(ledger.conflicts[conflict_offset:])
                result.conflicts.extend(
                    _constraint_conflict_evidence(adapter, conflict)
                    for conflict in result.constraint_conflicts
                )
                result.diagnostics.append("adapter_conflict")
                return result
        with write_owner(adapter_owner(adapter)):
            adapter.apply(data, schema, standard_sql, student_sql, ast_diffs)
        result.writes = _database_writes(before, data)
        _validate_adapter_writes(adapter, result.writes, declared_write_set)
        result.write_set_satisfied = True if declared_write_set else None
        result.applied = True
    except PermissionError as exc:
        _restore_database(data, before)
        result.write_set_satisfied = False
        result.conflicts.append({
            "adapter": adapter.name,
            "type": type(exc).__name__,
            "message": str(exc),
            "action": "split_world",
        })
        result.diagnostics.append("adapter_conflict")
    except Exception as exc:
        _restore_database(data, before)
        result.diagnostics.append(f"adapter_failed:{type(exc).__name__}:{exc}")
    return result


__all__ = [
    "AdapterRun",
    "LegacyProbeAdapter",
    "LegacyProbeRegistry",
    "adapter_owner",
    "run_adapter",
]
