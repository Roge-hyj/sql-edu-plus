"""Conflict-aware planning primitives for multi-world SQL witnesses.

The legacy generator still contains a large collection of data probes.  This
module gives those probes an isolation boundary: obligations are grouped into
compatible worlds first, and each world receives its own database.  The
planner is deliberately deterministic and bounded; it is not a general
constraint solver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterable

from .obligations import ConstraintSpec, DistinguishingObligation

_WRITE_OWNER: ContextVar[str] = ContextVar("witness_write_owner", default="")


@contextmanager
def write_owner(owner: str):
    token = _WRITE_OWNER.set(owner)
    try:
        yield
    finally:
        _WRITE_OWNER.reset(token)


@dataclass(frozen=True)
class CellConstraint:
    """One owned write target in a witness world.

    ``row_slot`` is a semantic role (for example ``boundary_row``), rather
    than an index chosen by a probe.  This keeps the declaration stable while
    the world builder decides how many physical rows to materialize.
    """

    table: str
    row_slot: str
    column: str
    relation: str
    value: Any = None
    owner: str = ""
    obligation_id: str = ""
    diff_id: str = ""
    priority: int = 0
    locked: bool = True

    @property
    def target(self) -> tuple[str, str, str]:
        return (
            self.table.strip().lower(),
            self.row_slot.strip().lower(),
            self.column.strip().lower(),
        )


@dataclass(frozen=True)
class ConstraintConflict:
    """A conflict that must be resolved by world splitting."""

    target: tuple[str, str, str]
    existing: CellConstraint
    incoming: CellConstraint
    reason: str


@dataclass(frozen=True)
class WriteEvent:
    table: str
    row_index: int
    column: str
    previous: Any
    value: Any
    owner: str = "legacy_probe"


class TrackedRow(dict[str, Any]):
    """Dict-compatible row that records legacy probe assignments."""

    def __init__(self, values: dict[str, Any], table: str, row_index: int, audit: list[WriteEvent], owner: str = "legacy_probe"):
        super().__init__(values)
        self._table = table
        self._row_index = row_index
        self._audit = audit
        self._owner = owner

    def __setitem__(self, key: str, value: Any) -> None:
        # ``dict`` subclasses are populated through ``__setitem__`` before
        # their instance attributes are restored during unpickling.  Worker
        # processes return witness databases through multiprocessing's
        # pickler, so this initialization path must not be treated as a probe
        # write (or assume that tracking metadata already exists).
        audit = getattr(self, "_audit", None)
        if audit is not None:
            owner = _WRITE_OWNER.get() or getattr(self, "_owner", "legacy_probe")
            audit.append(
                WriteEvent(
                    getattr(self, "_table", ""),
                    getattr(self, "_row_index", -1),
                    str(key),
                    self.get(key),
                    value,
                    owner,
                )
            )
        super().__setitem__(key, value)


def track_database_rows(database: dict[str, list[dict[str, Any]]], audit: list[WriteEvent], owner: str = "legacy_probe") -> None:
    for table, rows in database.items():
        for index, row in enumerate(rows):
            if not isinstance(row, TrackedRow):
                rows[index] = TrackedRow(row, table, index, audit, owner)


def summarize_write_audit(audit: list[WriteEvent]) -> dict[str, Any]:
    counts: dict[tuple[str, int, str], int] = {}
    for event in audit:
        key = (event.table.lower(), event.row_index, event.column.lower())
        counts[key] = counts.get(key, 0) + 1
    overwritten = [
        {"table": table, "row_index": index, "column": column, "writes": count}
        for (table, index, column), count in sorted(counts.items())
        if count > 1
    ]
    return {
        "write_count": len(audit),
        "unique_cells_written": len(counts),
        "overwritten_count": len(overwritten),
        "overwritten_cells": overwritten,
        "overwritten_by_other_owner_count": sum(
            1 for key in {
                (event.table.lower(), event.row_index, event.column.lower())
                for event in audit
            }
            if len({
                event.owner
                for event in audit
                if (event.table.lower(), event.row_index, event.column.lower()) == key
            }) > 1
        ),
        "owners": sorted({event.owner for event in audit}),
    }


class ConstraintLedger:
    """Track owned cell writes and reject silent overwrites."""

    def __init__(self) -> None:
        self._constraints: dict[tuple[str, str, str], CellConstraint] = {}
        self.conflicts: list[ConstraintConflict] = []

    @property
    def constraints(self) -> list[CellConstraint]:
        return list(self._constraints.values())

    def add(self, constraint: CellConstraint) -> bool:
        key = constraint.target
        existing = self._constraints.get(key)
        if existing is None:
            self._constraints[key] = constraint
            return True
        if _constraints_compatible(existing, constraint):
            # Preserve the strongest owner while retaining the deterministic
            # first value.  Equal constraints do not need a second write.
            if constraint.priority > existing.priority:
                self._constraints[key] = constraint
            return True
        self.conflicts.append(
            ConstraintConflict(
                target=key,
                existing=existing,
                incoming=constraint,
                reason="locked_cell_incompatible",
            )
        )
        return False

    def add_many(self, constraints: Iterable[CellConstraint]) -> bool:
        snapshot = dict(self._constraints)
        for constraint in constraints:
            if self.add(constraint):
                continue
            # A declaration either owns all of its required cells or none of
            # them.  Retaining a partial declaration would create exactly the
            # silent cross-strategy corruption this ledger is meant to stop.
            self._constraints = snapshot
            return False
        return True


@dataclass(frozen=True)
class StrategyDeclaration:
    """Planner-facing declaration compiled from one semantic obligation."""

    obligation_id: str
    diff_id: str
    strategy: str
    required_tables: frozenset[str]
    minimum_rows: tuple[tuple[str, int], ...]
    semantic_constraints: tuple[ConstraintSpec, ...]
    cell_constraints: tuple[CellConstraint, ...]
    conflict_families: frozenset[str]
    conflicts_with: frozenset[str]
    estimated_cost: int

    @property
    def row_requirements(self) -> dict[str, int]:
        return dict(self.minimum_rows)


@dataclass
class WitnessWorld:
    """One independent database candidate and its validation metadata."""

    id: str
    obligation_ids: list[str] = field(default_factory=list)
    diff_ids: list[str] = field(default_factory=list)
    constraints: list[CellConstraint] = field(default_factory=list)
    minimum_rows: dict[str, int] = field(default_factory=dict)
    database: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    execution: dict[str, Any] = field(default_factory=dict)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "world_id": self.id,
            "obligation_ids": list(self.obligation_ids),
            "diff_ids": list(self.diff_ids),
            "constraint_count": len(self.constraints),
            "constraints": [
                {
                    "table": item.table,
                    "row_slot": item.row_slot,
                    "column": item.column,
                    "relation": item.relation,
                    "value": item.value,
                    "owner": item.owner,
                    "obligation_id": item.obligation_id,
                    "diff_id": item.diff_id,
                    "priority": item.priority,
                    "locked": item.locked,
                }
                for item in self.constraints
            ],
            "minimum_rows": dict(self.minimum_rows),
            "diagnostics": list(self.diagnostics),
            "execution": dict(self.execution),
        }


@dataclass
class WitnessSuite:
    """All planned worlds for one standard/student SQL pair."""

    worlds: list[WitnessWorld]
    obligations: list[DistinguishingObligation]
    uncovered_obligations: list[str] = field(default_factory=list)
    planner_diagnostics: list[str] = field(default_factory=list)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "world_count": len(self.worlds),
            "worlds": [world.to_evidence() for world in self.worlds],
            "obligation_count": len(self.obligations),
            "obligation_ids": [item.id for item in self.obligations],
            "uncovered_obligations": list(self.uncovered_obligations),
            "planner_diagnostics": list(self.planner_diagnostics),
        }


def split_world_on_conflict(
    world: WitnessWorld,
    conflict: ConstraintConflict,
    *,
    right_world_id: str | None = None,
) -> tuple[WitnessWorld, WitnessWorld]:
    """Return two bounded candidates, each retaining one side of a conflict.

    This deterministic primitive is intentionally limited to conflicts already
    expressed as CellConstraints. It does not attempt to solve arbitrary
    legacy writes.
    """
    existing = conflict.existing
    incoming = conflict.incoming
    left = deepcopy(world)
    right = deepcopy(world)
    right.id = right_world_id or f"{world.id}_split"

    def replace_constraint(candidate, replacement, rejected):
        candidate.constraints = [
            item for item in candidate.constraints if item.target != replacement.target
        ]
        candidate.constraints.append(replacement)
        if (
            rejected.obligation_id
            and rejected.obligation_id != replacement.obligation_id
        ):
            candidate.obligation_ids = [
                obligation_id
                for obligation_id in candidate.obligation_ids
                if obligation_id != rejected.obligation_id
            ]
        if (
            rejected.diff_id
            and rejected.diff_id != replacement.diff_id
        ):
            candidate.diff_ids = [
                diff_id
                for diff_id in candidate.diff_ids
                if diff_id != rejected.diff_id
            ]
        if (
            replacement.obligation_id
            and replacement.obligation_id not in candidate.obligation_ids
        ):
            candidate.obligation_ids.append(replacement.obligation_id)
        if replacement.diff_id and replacement.diff_id not in candidate.diff_ids:
            candidate.diff_ids.append(replacement.diff_id)

    replace_constraint(left, existing, incoming)
    replace_constraint(right, incoming, existing)
    for candidate, side in ((left, "existing"), (right, "incoming")):
        candidate.diagnostics.append("world_split_from_constraint_conflict")
        candidate.execution.setdefault("world_splits", []).append({
            "conflict": True,
            "target": list(conflict.target),
            "owner_existing": existing.owner,
            "owner_incoming": incoming.owner,
            "selected_side": side,
            "action": "split_world",
        })
    return left, right


_STRATEGY_BY_KIND = {
    "boundary_tristate": "comparison_boundary_tristate",
    "null_safe_comparison_paths": "null_safe_comparison_paths",
    "regex_pattern_separation": "regex_pattern_separation",
    "like_pattern_separation": "like_pattern_separation",
    "glob_pattern_separation": "glob_pattern_separation",
    "similar_pattern_separation": "similar_pattern_separation",
    "boolean_truth_table": "logical_truth_table",
    "matched_and_dangling_join_rows": "join_dangling_rows",
    "standard_join_equal_student_join_unequal": "join_key_drift",
    "outer_join_predicate_placement_path": "join_predicate_placement",
    "group_grain_split": "group_grain_split",
    "aggregate_boundary_group": "aggregate_boundary_group",
    "filtered_aggregate_boundary_path": "filtered_aggregate_boundary_path",
    "aggregate_filter_paths": "aggregate_filter_paths",
    "aggregate_function_separation": "aggregate_function_separation",
    "scalar_subquery_boundary_path": "scalar_subquery_boundary_path",
    "set_left_right_overlap": "set_overlap",
    "window_partitions_and_ties": "window_partition_ties",
    "case_unmatched_and_branch_rows": "case_branch_coverage",
    "subquery_membership_paths": "subquery_membership_paths",
    "in_list_membership_paths": "in_list_membership_paths",
    "predicate_positive_negative_paths": "predicate_positive_negative",
    "observable_projection_discriminator": "observable_projection_discriminator",
    "order_key_separation": "order_key_separation",
    "projection_shape_paths": "projection_shape_check",
    "projection_value_paths": "projection_value_check",
    "projection_boolean_tristate_paths": "projection_boolean_tristate",
    "limit_row_count_paths": "limit_row_count_boundary",
    "cte_base_paths": "cte_base_paths",
    "cte_base_recursive_orphan_paths": "cte_recursive_paths",
    "null_and_non_null_rows": "null_tristate",
    "null_predicate_paths": "null_tristate",
    "duplicate_projected_tuple": "duplicate_projection",
    "distinct_on_competing_payload": "distinct_on_competing_payload",
}

_FAMILY_BY_KIND = {
    "matched_and_dangling_join_rows": "join",
    "standard_join_equal_student_join_unequal": "join",
    "outer_join_predicate_placement_path": "join",
    "group_grain_split": "aggregate",
    "aggregate_boundary_group": "aggregate",
    "filtered_aggregate_boundary_path": "aggregate",
    "aggregate_filter_paths": "aggregate",
    "aggregate_function_separation": "aggregate",
    "scalar_subquery_boundary_path": "subquery",
    "window_partitions_and_ties": "window",
    "duplicate_projected_tuple": "distinct",
    "distinct_on_competing_payload": "distinct",
    "cte_base_recursive_orphan_paths": "recursive",
    "set_left_right_overlap": "set",
    "subquery_membership_paths": "subquery",
}

# These families frequently compete for the same physical row topology.  They
# are deliberately split even when their table sets happen to be disjoint;
# keeping the worlds small makes the execution evidence easier to interpret.
_INCOMPATIBLE_FAMILIES = frozenset(
    frozenset(pair)
    for pair in (
        ("join", "window"),
        ("join", "aggregate"),
        ("join", "distinct"),
        ("join", "recursive"),
        ("aggregate", "window"),
        ("aggregate", "distinct"),
        ("aggregate", "recursive"),
        ("window", "distinct"),
        ("window", "recursive"),
        ("distinct", "recursive"),
    )
)


def _projection_boolean_path_values(
    metadata: dict[str, Any],
) -> tuple[bool, Any, Any]:
    """Return one TRUE and one FALSE input for a simple nullable predicate."""
    operator = str(metadata.get("predicate_operator") or "").upper()
    boundary = metadata.get("predicate_value")
    if operator == "COLUMN":
        return True, 1, 0
    if operator not in {"EQ", "NEQ", "GT", "GTE", "LT", "LTE"}:
        return False, None, None
    if boundary is None or isinstance(boundary, bool):
        return False, None, None

    if isinstance(boundary, (int, float)):
        below, above = boundary - 1, boundary + 1
    elif isinstance(boundary, str):
        below = "" if boundary else None
        above = f"{boundary}~"
    else:
        return False, None, None

    if operator == "EQ":
        return True, boundary, above
    if operator == "NEQ":
        return True, above, boundary
    if operator == "GT":
        return True, above, boundary
    if operator == "GTE" and below is not None:
        return True, boundary, below
    if operator == "LT" and below is not None:
        return True, below, boundary
    if operator == "LTE":
        return True, boundary, above
    return False, None, None


def _semantic_cell_constraints(obligation: DistinguishingObligation) -> list[CellConstraint]:
    """Turn only concrete requirements into owned cell declarations.

    Most obligations are intentionally semantic (for example "produce a
    tie").  They remain in ``semantic_constraints`` and are handled by the
    compatibility generator until their dedicated strategy is migrated.  A
    few exact requirements are safe to materialize here immediately.
    """

    table = next(iter(sorted(obligation.required_tables)), "")
    column = next(
        (item.column for item in sorted(obligation.required_columns)),
        "",
    )
    result: list[CellConstraint] = []
    for spec in obligation.hard_constraints:
        if spec.kind == "window_partitions_and_ties":
            metadata = dict(spec.metadata)
            standard_items = tuple(metadata.get("standard_window_order_items") or ())
            student_items = tuple(metadata.get("student_window_order_items") or ())
            changed_index = next(
                (
                    index
                    for index, (standard_item, student_item) in enumerate(
                        zip(standard_items, student_items)
                    )
                    if len(standard_item) >= 3
                    and len(student_item) >= 3
                    and bool(standard_item[2]) != bool(student_item[2])
                ),
                None,
            )
            order_columns = tuple(
                metadata.get("standard_window_order_columns")
                or metadata.get("student_window_order_columns")
                or ()
            )
            if changed_index is not None and changed_index < len(order_columns):
                order_column = str(order_columns[changed_index]).split(".")[-1].strip('`" ')
                target_table = str(
                    metadata.get("standard_window_source_table")
                    or metadata.get("student_window_source_table")
                    or next(iter(sorted(obligation.required_tables)), "")
                )
                if target_table and order_column:
                    owner = _STRATEGY_BY_KIND[spec.kind]
                    result.extend(
                        (
                            CellConstraint(
                                table=target_table,
                                row_slot="row_slot_0",
                                column=order_column,
                                relation="is_null",
                                owner=owner,
                                obligation_id=obligation.id,
                                diff_id=obligation.diff_id,
                                priority=95,
                                locked=True,
                            ),
                            CellConstraint(
                                table=target_table,
                                row_slot="row_slot_1",
                                column=order_column,
                                relation="equals",
                                value=10,
                                owner=owner,
                                obligation_id=obligation.id,
                                diff_id=obligation.diff_id,
                                priority=95,
                                locked=True,
                            ),
                            CellConstraint(
                                table=target_table,
                                row_slot="row_slot_2",
                                column=order_column,
                                relation="equals",
                                value=20,
                                owner=owner,
                                obligation_id=obligation.id,
                                diff_id=obligation.diff_id,
                                priority=95,
                                locked=True,
                            ),
                        )
                    )
            # A pure PARTITION BY change has no order key to lock.  Its
            # partition topology remains semantic and is materialized by the
            # dedicated window probe.
            continue
        if not table or not column:
            continue
        if (
            spec.kind == "boundary_tristate"
            and spec.value is not None
            and str(dict(spec.metadata).get("standard_value_kind") or "").lower()
            != "expression"
        ):
            result.append(
                CellConstraint(
                    table=table,
                    row_slot="boundary_row",
                    column=column,
                    relation="equals",
                    value=spec.value,
                    owner=_STRATEGY_BY_KIND.get(spec.kind, spec.kind),
                    obligation_id=obligation.id,
                    diff_id=obligation.diff_id,
                    priority=80,
                    locked=True,
                )
            )
        elif spec.kind == "null_and_non_null_rows":
            result.extend(
                (
                    CellConstraint(
                        table=table,
                        row_slot="null_row",
                        column=column,
                        relation="is_null",
                        owner=_STRATEGY_BY_KIND[spec.kind],
                        obligation_id=obligation.id,
                        diff_id=obligation.diff_id,
                        priority=80,
                        locked=True,
                    ),
                    CellConstraint(
                        table=table,
                        row_slot="non_null_row",
                        column=column,
                        relation="not_null",
                        owner=_STRATEGY_BY_KIND[spec.kind],
                        obligation_id=obligation.id,
                        diff_id=obligation.diff_id,
                        priority=80,
                        locked=True,
                    ),
                )
            )
        elif spec.kind == "null_safe_comparison_paths":
            metadata = dict(spec.metadata)
            right_column = str(
                metadata.get("standard_right_column") or ""
            )
            same_right_column = bool(metadata.get("same_right_column"))
            if same_right_column and right_column and right_column.lower() != spec.column.lower():
                owner = _STRATEGY_BY_KIND[spec.kind]
                for row_slot, left_relation, right_relation in (
                    ("row_slot_0", "is_null", "is_null"),
                    ("row_slot_1", "is_null", "not_null"),
                    ("row_slot_2", "equals_column", "not_null"),
                    ("row_slot_3", "not_equals_column", "not_null"),
                ):
                    result.append(CellConstraint(
                        table=table,
                        row_slot=row_slot,
                        column=right_column,
                        relation=right_relation,
                        owner=owner,
                        obligation_id=obligation.id,
                        diff_id=obligation.diff_id,
                        priority=85,
                        locked=True,
                    ))
                    result.append(CellConstraint(
                        table=table,
                        row_slot=row_slot,
                        column=spec.column,
                        relation=left_relation,
                        value=(
                            right_column
                            if left_relation in {"equals_column", "not_equals_column"}
                            else None
                        ),
                        owner=owner,
                        obligation_id=obligation.id,
                        diff_id=obligation.diff_id,
                        priority=85,
                        locked=True,
                    ))
                continue
            result.append(
                CellConstraint(
                    table=table,
                    row_slot="null_row",
                    column=spec.column or column,
                    relation="is_null",
                    owner=_STRATEGY_BY_KIND[spec.kind],
                    obligation_id=obligation.id,
                    diff_id=obligation.diff_id,
                    priority=85,
                    locked=True,
                )
            )
            if spec.value is None:
                result.append(
                    CellConstraint(
                        table=table,
                        row_slot="last_row",
                        column=spec.column or column,
                        relation="not_null",
                        owner=_STRATEGY_BY_KIND[spec.kind],
                        obligation_id=obligation.id,
                        diff_id=obligation.diff_id,
                        priority=85,
                        locked=True,
                    )
                )
            else:
                result.append(
                    CellConstraint(
                        table=table,
                        row_slot="boundary_row",
                        column=spec.column or column,
                        relation="equals",
                        value=spec.value,
                        owner=_STRATEGY_BY_KIND[spec.kind],
                        obligation_id=obligation.id,
                        diff_id=obligation.diff_id,
                        priority=85,
                        locked=True,
                    )
                )
        elif spec.kind == "projection_boolean_tristate_paths":
            metadata = dict(spec.metadata)
            supported, true_value, false_value = _projection_boolean_path_values(
                metadata
            )
            owner = _STRATEGY_BY_KIND[spec.kind]
            result.append(CellConstraint(
                table=table,
                row_slot="row_slot_0",
                column=spec.column or column,
                relation="is_null",
                owner=owner,
                obligation_id=obligation.id,
                diff_id=obligation.diff_id,
                priority=90,
                locked=True,
            ))
            if supported:
                for row_slot, value in (
                    ("row_slot_1", true_value),
                    ("row_slot_2", false_value),
                ):
                    result.append(CellConstraint(
                        table=table,
                        row_slot=row_slot,
                        column=spec.column or column,
                        relation="equals",
                        value=value,
                        owner=owner,
                        obligation_id=obligation.id,
                        diff_id=obligation.diff_id,
                        priority=90,
                        locked=True,
                    ))
    return result


def declare_strategy(obligation: DistinguishingObligation) -> StrategyDeclaration:
    kinds = tuple(item.kind for item in obligation.hard_constraints)
    strategy = _STRATEGY_BY_KIND.get(kinds[0], kinds[0] if kinds else obligation.diff_type)
    families = frozenset(
        family
        for family in (_FAMILY_BY_KIND.get(kind) for kind in kinds)
        if family
    )
    return StrategyDeclaration(
        obligation_id=obligation.id,
        diff_id=obligation.diff_id,
        strategy=strategy,
        required_tables=frozenset(obligation.required_tables),
        minimum_rows=tuple(sorted(obligation.minimum_rows.items())),
        semantic_constraints=tuple(obligation.hard_constraints),
        cell_constraints=tuple(_semantic_cell_constraints(obligation)),
        conflict_families=families,
        conflicts_with=frozenset(obligation.conflicts_with),
        estimated_cost=obligation.estimated_cost,
    )


def _constraints_compatible(left: CellConstraint, right: CellConstraint) -> bool:
    if left.relation == right.relation and left.value == right.value:
        return True
    # A symbolic requirement cannot disprove a concrete one.  Symbolic
    # constraints are kept for diagnostics but do not cause a false split.
    if left.value is None or right.value is None:
        return left.relation == right.relation
    if {left.relation, right.relation} == {"is_null", "not_null"}:
        return False
    return False


def _declarations_conflict(
    left: StrategyDeclaration,
    right: StrategyDeclaration,
) -> bool:
    if left.obligation_id == right.obligation_id:
        return False
    if (
        right.obligation_id in left.conflicts_with
        or left.obligation_id in right.conflicts_with
    ):
        return True
    if not left.conflict_families or not right.conflict_families:
        return False
    return any(
        frozenset((left_family, right_family)) in _INCOMPATIBLE_FAMILIES
        for left_family in left.conflict_families
        for right_family in right.conflict_families
    )


class WitnessPlanner:
    """Deterministically pack compatible declarations into bounded worlds."""

    def __init__(
        self,
        *,
        max_worlds: int = 8,
        isolate_obligations: bool = True,
    ) -> None:
        self.max_worlds = max(1, int(max_worlds))
        # During migration, one obligation per world gives unambiguous probe
        # effectiveness evidence.  Packing remains available for later stages
        # once obligation-specific success predicates are executable.
        self.isolate_obligations = isolate_obligations

    def plan(self, obligations: Iterable[DistinguishingObligation]) -> WitnessSuite:
        ordered = sorted(obligations, key=lambda item: (item.estimated_cost, item.id))
        worlds: list[WitnessWorld] = []
        ledgers: list[ConstraintLedger] = []
        declarations = [declare_strategy(item) for item in ordered]
        declaration_by_id = {item.obligation_id: item for item in declarations}
        uncovered: list[str] = []
        diagnostics: list[str] = []

        for declaration in declarations:
            placed = False
            candidate_worlds = [] if self.isolate_obligations else list(enumerate(worlds))
            for index, world in candidate_worlds:
                existing = [declaration_by_id[item] for item in world.obligation_ids]
                if any(_declarations_conflict(item, declaration) for item in existing):
                    continue
                ledger = ledgers[index]
                before_conflicts = len(ledger.conflicts)
                if not ledger.add_many(declaration.cell_constraints):
                    # Roll back only the candidate's newly recorded conflicts;
                    # the world itself remains valid for its prior obligations.
                    del ledger.conflicts[before_conflicts:]
                    continue
                world.obligation_ids.append(declaration.obligation_id)
                world.diff_ids.append(declaration.diff_id)
                world.constraints = ledger.constraints
                for table, count in declaration.row_requirements.items():
                    world.minimum_rows[table] = max(world.minimum_rows.get(table, 0), count)
                placed = True
                break
            if placed:
                continue
            if len(worlds) >= self.max_worlds:
                uncovered.append(declaration.obligation_id)
                diagnostics.append(
                    f"world_limit_reached:{declaration.obligation_id}"
                )
                continue
            world = WitnessWorld(id=f"world_{len(worlds) + 1:02d}")
            ledger = ConstraintLedger()
            ledger.add_many(declaration.cell_constraints)
            world.obligation_ids.append(declaration.obligation_id)
            world.diff_ids.append(declaration.diff_id)
            world.constraints = ledger.constraints
            world.minimum_rows.update(declaration.row_requirements)
            if ledger.conflicts:
                world.diagnostics.extend(
                    f"constraint_conflict:{item.reason}" for item in ledger.conflicts
                )
            worlds.append(world)
            ledgers.append(ledger)

        if not worlds:
            worlds.append(WitnessWorld(id="world_01"))
            diagnostics.append("no_obligations_base_world")
        return WitnessSuite(
            worlds=worlds,
            obligations=ordered,
            uncovered_obligations=uncovered,
            planner_diagnostics=diagnostics,
        )


def _row_index(row_slot: str, row_count: int) -> int | None:
    if row_count <= 0:
        return None
    normalized = row_slot.strip().lower()
    if normalized in {"first_row", "null_row", "below_boundary"}:
        return 0
    if normalized in {"second_row", "non_null_row", "boundary_row", "match_row"}:
        return min(1, row_count - 1)
    if normalized in {"last_row", "dangling_row", "unmatched_row"}:
        return row_count - 1
    match = re.search(r"(?:row|slot)[_-]?(\d+)$", normalized)
    if match:
        index = int(match.group(1))
        return index if index < row_count else None
    return None


def apply_cell_constraints(
    database: dict[str, list[dict[str, Any]]],
    constraints: Iterable[CellConstraint],
) -> dict[str, Any]:
    """Materialize concrete declarations and report what could not be applied."""

    applied: list[dict[str, Any]] = []
    unsatisfied: list[dict[str, Any]] = []
    table_lookup = {name.lower(): name for name in database}
    for constraint in constraints:
        table_name = table_lookup.get(constraint.table.lower())
        if not table_name or not database[table_name]:
            unsatisfied.append({"constraint": constraint.__dict__, "reason": "table_or_rows_missing"})
            continue
        rows = database[table_name]
        index = _row_index(constraint.row_slot, len(rows))
        if index is None:
            unsatisfied.append({"constraint": constraint.__dict__, "reason": "row_slot_missing"})
            continue
        column = next(
            (name for name in rows[index] if name.lower() == constraint.column.lower()),
            None,
        )
        if not column:
            unsatisfied.append({"constraint": constraint.__dict__, "reason": "column_missing"})
            continue
        before = rows[index].get(column)
        if constraint.relation == "equals":
            rows[index][column] = constraint.value
        elif constraint.relation == "is_null":
            rows[index][column] = None
        elif constraint.relation == "not_null":
            if rows[index].get(column) is None:
                rows[index][column] = 0
        elif constraint.relation in {"equals_column", "not_equals_column"}:
            referenced_column = next(
                (
                    name
                    for name in rows[index]
                    if name.lower() == str(constraint.value or "").lower()
                ),
                None,
            )
            if not referenced_column:
                unsatisfied.append({
                    "constraint": constraint.__dict__,
                    "reason": "referenced_column_missing",
                })
                continue
            referenced_value = rows[index].get(referenced_column)
            if constraint.relation == "equals_column":
                rows[index][column] = referenced_value
            else:
                candidate = rows[index].get(column)
                if candidate is None or candidate == referenced_value:
                    candidate = next(
                        (
                            row.get(column)
                            for row in rows
                            if row.get(column) is not None
                            and row.get(column) != referenced_value
                        ),
                        None,
                    )
                if candidate is None or candidate == referenced_value:
                    if isinstance(referenced_value, bool):
                        candidate = not referenced_value
                    elif isinstance(referenced_value, (int, float)):
                        candidate = referenced_value + 1
                    elif isinstance(referenced_value, str):
                        candidate = f"{referenced_value}__other"
                    else:
                        candidate = 1 if referenced_value != 1 else 2
                rows[index][column] = candidate
        else:
            unsatisfied.append({"constraint": constraint.__dict__, "reason": "unsupported_relation"})
            continue
        applied.append(
            {
                "table": table_name,
                "row_index": index,
                "column": column,
                "before": before,
                "after": rows[index].get(column),
                "owner": constraint.owner,
                "obligation_id": constraint.obligation_id,
                "diff_id": constraint.diff_id,
            }
        )
    return {
        "applied": applied,
        "unsatisfied": unsatisfied,
        "constraints_satisfied": not unsatisfied,
        # The ledger rejects conflicting writers before materialization.  A
        # value changing from its seed to its declared value is an expected
        # application, not an overwrite by another strategy.
        "overwritten": False,
        "writes_applied": len(applied),
    }


def apply_bounded_feedback(
    database: dict[str, list[dict[str, Any]]],
    obligations: Iterable[DistinguishingObligation],
    *,
    attempt: int,
) -> dict[str, Any]:
    """Expand a small candidate domain around unresolved obligations.

    Only tables and columns named by an obligation may be touched.  This is a
    deterministic compatibility bridge for the first migrated strategies; it
    intentionally declines unsupported semantic constraints instead of
    growing unrelated tables or invoking a general-purpose solver.
    """

    adjustments: list[dict[str, Any]] = []
    unsupported: list[dict[str, str]] = []
    for obligation in obligations:
        target = _obligation_data_target(database, obligation)
        if target is None:
            unsupported.append(
                {"obligation_id": obligation.id, "reason": "target_not_materialized"}
            )
            continue
        table_name, rows, column = target
        handled = False

        def assign(index: int, value: Any) -> None:
            # Keep bounded feedback visible to the same write-audit path as
            # legacy probes.  This is still a compatibility bridge, but it
            # must not become an untracked fourth writer category.
            with write_owner(f"feedback:{obligation.id}"):
                rows[index][column] = value

        for spec in obligation.hard_constraints:
            before = [row.get(column) for row in rows]
            if (
                spec.kind == "boundary_tristate"
                and spec.value is not None
                and len(rows) >= 3
                and str(dict(spec.metadata).get("standard_value_kind") or "").lower()
                != "expression"
            ):
                value = spec.value
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    distance = max(1, attempt)
                    values = (value - distance, value, value + distance)
                else:
                    values = (f"{value}__below", value, f"{value}__above")
                for index, candidate in enumerate(values):
                    assign(index, candidate)
                handled = True
            elif spec.kind == "null_and_non_null_rows" and len(rows) >= 2:
                assign(0, None)
                if rows[1].get(column) is None:
                    assign(1, attempt)
                handled = True
            elif spec.kind == "null_safe_comparison_paths" and len(rows) >= 3:
                metadata = dict(spec.metadata)
                right_requested = str(
                    metadata.get("standard_right_column") or ""
                )
                right_column = next(
                    (
                        name
                        for name in rows[0]
                        if name.lower() == right_requested.lower()
                    ),
                    None,
                )
                same_right_column = bool(metadata.get("same_right_column"))
                if (
                    same_right_column
                    and right_column
                    and right_column.lower() != column.lower()
                    and len(rows) >= 4
                ):
                    def assign_right(index: int, value: Any) -> None:
                        with write_owner(f"feedback:{obligation.id}"):
                            rows[index][right_column] = value

                    def existing_non_null(index: int, *columns: str) -> Any:
                        for candidate_column in columns:
                            candidate = rows[index].get(candidate_column)
                            if candidate is not None:
                                return candidate
                        for row in rows:
                            for candidate_column in columns:
                                candidate = row.get(candidate_column)
                                if candidate is not None:
                                    return candidate
                        return attempt

                    assign(0, None)
                    assign_right(0, None)
                    one_sided = existing_non_null(1, right_column, column)
                    assign(1, None)
                    assign_right(1, one_sided)
                    equal_value = existing_non_null(2, right_column, column)
                    assign(2, equal_value)
                    assign_right(2, equal_value)
                    unequal_right = existing_non_null(3, right_column, column)
                    unequal_left = next(
                        (
                            row.get(column)
                            for row in rows
                            if row.get(column) is not None
                            and row.get(column) != unequal_right
                        ),
                        None,
                    )
                    if unequal_left is None:
                        if isinstance(unequal_right, (int, float)) and not isinstance(unequal_right, bool):
                            unequal_left = unequal_right + max(1, attempt)
                        else:
                            unequal_left = f"{unequal_right}__other_{attempt}"
                    assign(3, unequal_left)
                    assign_right(3, unequal_right)
                else:
                    assign(0, None)
                    if spec.value is None:
                        assign(1, attempt)
                        assign(2, attempt + 1)
                    else:
                        assign(1, spec.value)
                        if isinstance(spec.value, (int, float)) and not isinstance(spec.value, bool):
                            assign(2, spec.value + max(1, attempt))
                        else:
                            assign(2, f"{spec.value}__other_{attempt}")
                handled = True
            elif spec.kind == "projection_boolean_tristate_paths" and len(rows) >= 3:
                supported, true_value, false_value = _projection_boolean_path_values(
                    dict(spec.metadata)
                )
                if supported:
                    assign(0, None)
                    assign(1, true_value)
                    assign(2, false_value)
                    handled = True
            elif spec.kind == "duplicate_projected_tuple" and (
                obligation.diff_type == "aggregate_distinct_changed"
                and _identity_like_column(column)
            ):
                # COUNT(DISTINCT primary-key) and COUNT(primary-key) are
                # equivalent under the declared identity layout. Do not
                # manufacture an invalid duplicate merely to force a witness.
                handled = False
            elif spec.kind in {"window_partitions_and_ties", "duplicate_projected_tuple"} and len(rows) >= 2:
                anchor = rows[0].get(column)
                if spec.kind == "duplicate_projected_tuple":
                    numeric_values = [
                        item.get(column)
                        for item in rows
                        if isinstance(item.get(column), (int, float))
                        and not isinstance(item.get(column), bool)
                    ]
                    if numeric_values:
                        anchor = max(numeric_values) + max(1, attempt)
                assign(0, anchor)
                assign(1, anchor)
                handled = True
            elif spec.kind == "group_grain_split" and len(rows) >= 3:
                anchor = rows[0].get(column)
                assign(1, anchor)
                if isinstance(anchor, (int, float)) and not isinstance(anchor, bool):
                    assign(2, anchor + max(1, attempt))
                else:
                    assign(2, f"{anchor}__group_{attempt}")
                handled = True
            if handled:
                after = [row.get(column) for row in rows]
                adjustments.append(
                    {
                        "obligation_id": obligation.id,
                        "constraint_kind": spec.kind,
                        "table": table_name,
                        "column": column,
                        "attempt": attempt,
                        "before": before[:3],
                        "after": after[:3],
                    }
                )
                break
        if not handled:
            unsupported.append(
                {"obligation_id": obligation.id, "reason": "no_bounded_feedback_adapter"}
            )
    return {
        "attempt": attempt,
        "adjustments": adjustments,
        "unsupported": unsupported,
        "targeted": bool(adjustments),
    }


def _identity_like_column(column: str) -> bool:
    normalized = str(column or "").strip().lower()
    return normalized in {"id", "pk", "primary_key"} or normalized.endswith("_id")


def _obligation_data_target(
    database: dict[str, list[dict[str, Any]]],
    obligation: DistinguishingObligation,
) -> tuple[str, list[dict[str, Any]], str] | None:
    table_lookup = {name.lower(): name for name in database}
    preferred_columns = {
        str(spec.column).lower()
        for spec in obligation.hard_constraints
        if spec.column
    }
    column_refs = sorted(
        obligation.required_columns,
        key=lambda item: (
            0 if item.column.lower() in preferred_columns else 1,
            item,
        ),
    )
    for reference in column_refs:
        candidate_tables: list[str] = []
        if reference.relation:
            table_name = table_lookup.get(reference.relation.lower())
            if table_name:
                candidate_tables.append(table_name)
        if not candidate_tables:
            candidate_tables.extend(
                table_lookup[name.lower()]
                for name in sorted(obligation.required_tables)
                if name.lower() in table_lookup
            )
        if not candidate_tables:
            candidate_tables.extend(sorted(database))
        matches: list[tuple[str, list[dict[str, Any]], str]] = []
        for table_name in candidate_tables:
            rows = database.get(table_name) or []
            if not rows:
                continue
            column = next(
                (name for name in rows[0] if name.lower() == reference.column.lower()),
                None,
            )
            if column:
                matches.append((table_name, rows, column))
        if len(matches) == 1:
            return matches[0]
    return None


__all__ = [
    "CellConstraint",
    "ConstraintConflict",
    "ConstraintLedger",
    "StrategyDeclaration",
    "WitnessPlanner",
    "WitnessSuite",
    "WitnessWorld",
    "apply_bounded_feedback",
    "apply_cell_constraints",
    "declare_strategy",
    "summarize_write_audit",
    "track_database_rows",
    "WriteEvent",
    "write_owner",
]
