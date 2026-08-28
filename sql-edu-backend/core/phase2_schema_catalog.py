"""Bounded, defensive SchemaCatalog adapter for Phase 2 diagnosis.

The adapter accepts the JSON-shaped schema metadata already produced by the
online question preview and Phase 1 corpus jobs.  It deliberately does not
parse SQL or retain preview rows.  Missing metadata remains unknown: Phase 2
must not turn naming conventions into PK/FK facts.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from itertools import islice
import json
import re
from typing import Any, Mapping, Sequence


MAX_INPUT_BYTES = 256 * 1024
MAX_TABLES = 32
MAX_COLUMNS_PER_TABLE = 128
MAX_TOTAL_COLUMNS = 1024
MAX_CONSTRAINTS_PER_TABLE = 128
MAX_IDENTIFIER_CHARS = 128
MAX_TYPE_CHARS = 96
MAX_PUBLIC_OUTPUT_BYTES = 128 * 1024

_IDENTIFIER_PART = r"(?:[^\W\d][\w$]*|_[\w$]*)"
_IDENTIFIER = re.compile(rf"^{_IDENTIFIER_PART}$", re.UNICODE)
_QUALIFIED_IDENTIFIER = re.compile(
    rf"^{_IDENTIFIER_PART}(?:\.{_IDENTIFIER_PART})*$", re.UNICODE
)
_SQL_SHAPED_IDENTIFIER = re.compile(
    r"\b(?:select\b[\s\S]{0,300}\bfrom\b|insert\s+into\b|"
    r"update\b[\s\S]{0,160}\bset\b|delete\s+from\b|"
    r"drop\s+(?:table|database)\b|with\b[\s\S]{0,300}\bselect\b)",
    re.IGNORECASE,
)
_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(?:\(\s*\d{1,5}(?:\s*,\s*\d{1,5})?\s*\))?$" )
_ALLOWED_TYPES = {
    "BIGINT", "BINARY", "BLOB", "BOOL", "BOOLEAN", "CHAR", "CHARACTER",
    "CHARACTER VARYING", "CLOB", "DATE", "DATETIME", "DECIMAL", "DOUBLE",
    "DOUBLE PRECISION", "FLOAT", "INT", "INTEGER", "JSON", "NCHAR", "NTEXT",
    "NUMBER", "NUMERIC", "NVARCHAR", "REAL", "SMALLINT", "TEXT", "TIME",
    "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITHOUT TIME ZONE",
    "TINYINT", "UUID", "VARBINARY", "VARCHAR",
}
_PUBLIC_LIMITATION_CODES = frozenset({
    "CONSTRAINTS_AND_TYPES_UNKNOWN",
    "DUPLICATE_TABLE",
    "INVALID_COLUMN_TYPE_DROPPED",
    "INVALID_FOREIGN_KEY_DROPPED",
    "INVALID_OR_DUPLICATE_COLUMN",
    "INVALID_PRIMARY_KEY_DROPPED",
    "INVALID_TABLE_DROPPED",
    "INVALID_TABLE_STRUCTURE",
    "INVALID_UNIQUE_CONSTRAINT_DROPPED",
    "PUBLIC_SCHEMA_SUMMARY_TRUNCATED",
    "SCHEMA_INPUT_TOO_LARGE",
    "SCHEMA_INPUT_UNSUPPORTED",
    "SCHEMA_JSON_INVALID",
    "SCHEMA_LIMIT_EXCEEDED",
    "SCHEMA_TABLES_MISSING",
    "SCHEMA_TABLES_UNUSABLE",
    "UNRESOLVED_FOREIGN_KEY_DROPPED",
})


class SchemaConfidence(str, Enum):
    DECLARED = "DECLARED"
    STRUCTURE_ONLY = "STRUCTURE_ONLY"
    UNKNOWN = "UNKNOWN"


class JoinCardinality(str, Enum):
    ONE_TO_ONE = "ONE_TO_ONE"
    MANY_TO_ONE = "MANY_TO_ONE"
    ONE_TO_MANY = "ONE_TO_MANY"
    MANY_TO_MANY = "MANY_TO_MANY"
    UNKNOWN = "UNKNOWN"


def _norm(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _seq(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _identifier(value: Any, *, qualified: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_IDENTIFIER_CHARS:
        return None
    pattern = _QUALIFIED_IDENTIFIER if qualified else _IDENTIFIER
    if not pattern.fullmatch(value):
        return None
    decoded = re.sub(r"[_.$]+", " ", value)
    return None if _SQL_SHAPED_IDENTIFIER.search(decoded) else value


def _data_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value.strip())
    if not value or len(value) > MAX_TYPE_CHARS or not _TYPE.fullmatch(value):
        return None
    normalized = value.upper()
    base = normalized.split("(", 1)[0].strip()
    return normalized if base in _ALLOWED_TYPES else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class ColumnFact:
    name: str
    data_type: str | None = None
    nullable: bool | None = None
    primary_key: bool = False
    unique: bool = False

    def public_fact(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "data_type": self.data_type,
            "nullable": self.nullable,
            "primary_key": self.primary_key,
            "unique": self.unique,
        }


@dataclass(frozen=True)
class ForeignKeyFact:
    columns: tuple[str, ...]
    references_table: str
    references_columns: tuple[str, ...]

    def public_fact(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "references_table": self.references_table,
            "references_columns": list(self.references_columns),
        }


@dataclass(frozen=True)
class TableFact:
    name: str
    columns: tuple[ColumnFact, ...]
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKeyFact, ...] = ()
    unique_constraints: tuple[tuple[str, ...], ...] = ()
    confidence: SchemaConfidence = SchemaConfidence.STRUCTURE_ONLY

    def column(self, name: str) -> ColumnFact | None:
        key = _norm(name)
        return next((item for item in self.columns if _norm(item.name) == key), None)

    def uniquely_identified_by(self, columns: Sequence[str]) -> bool | None:
        """Whether the supplied grain contains a declared unique key."""
        if self.confidence is not SchemaConfidence.DECLARED:
            return None
        supplied = {_norm(item) for item in columns}
        keys = list(self.unique_constraints)
        if self.primary_key and self.primary_key not in keys:
            keys.append(self.primary_key)
        if not keys:
            return None
        return any({_norm(item) for item in key}.issubset(supplied) for key in keys)

    def public_fact(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "confidence": self.confidence.value,
            "columns": [item.public_fact() for item in self.columns],
            "primary_key": list(self.primary_key),
            "unique_constraints": [list(item) for item in self.unique_constraints],
            "foreign_keys": [item.public_fact() for item in self.foreign_keys],
        }


@dataclass(frozen=True)
class Phase2SchemaCatalog:
    tables: tuple[TableFact, ...] = ()
    confidence: SchemaConfidence = SchemaConfidence.UNKNOWN
    limitations: tuple[str, ...] = ()

    @classmethod
    def unknown(cls, reason: str) -> "Phase2SchemaCatalog":
        return cls(limitations=(reason,))

    @classmethod
    def from_input(cls, value: Any) -> "Phase2SchemaCatalog":
        return parse_schema_catalog(value)

    def table(self, name: str) -> TableFact | None:
        key = _norm(name)
        return next((item for item in self.tables if _norm(item.name) == key), None)

    def foreign_keys_from(self, table: str) -> tuple[ForeignKeyFact, ...]:
        found = self.table(table)
        return found.foreign_keys if found else ()

    def foreign_keys_between(
        self, left: str, right: str
    ) -> tuple[tuple[str, ForeignKeyFact], ...]:
        left_table, right_table = self.table(left), self.table(right)
        if left_table is None or right_table is None:
            return ()
        result: list[tuple[str, ForeignKeyFact]] = []
        for owner, target in ((left_table, right_table), (right_table, left_table)):
            result.extend(
                (owner.name, key)
                for key in owner.foreign_keys
                if _norm(key.references_table) == _norm(target.name)
            )
        return tuple(sorted(result, key=lambda item: (_norm(item[0]), item[1].columns)))

    def uniquely_identifies(self, table: str, columns: Sequence[str]) -> bool | None:
        found = self.table(table)
        return found.uniquely_identified_by(columns) if found else None

    def join_cardinality(self, left: str, right: str) -> JoinCardinality:
        """Return cardinality from the left table's perspective."""
        left_table, right_table = self.table(left), self.table(right)
        if left_table is None or right_table is None:
            return JoinCardinality.UNKNOWN
        forward = [
            key for key in left_table.foreign_keys
            if _norm(key.references_table) == _norm(right_table.name)
        ]
        reverse = [
            key for key in right_table.foreign_keys
            if _norm(key.references_table) == _norm(left_table.name)
        ]
        if forward:
            unique = any(
                left_table.uniquely_identified_by(key.columns) is True
                for key in forward
            )
            return JoinCardinality.ONE_TO_ONE if unique else JoinCardinality.MANY_TO_ONE
        if reverse:
            unique = any(
                right_table.uniquely_identified_by(key.columns) is True
                for key in reverse
            )
            return JoinCardinality.ONE_TO_ONE if unique else JoinCardinality.ONE_TO_MANY
        return JoinCardinality.UNKNOWN

    def may_fan_out(self, base: str, joined: str) -> bool | None:
        cardinality = self.join_cardinality(base, joined)
        if cardinality is JoinCardinality.UNKNOWN:
            return None
        return cardinality in {JoinCardinality.ONE_TO_MANY, JoinCardinality.MANY_TO_MANY}

    def bridge_tables(self, left: str, right: str) -> tuple[str, ...]:
        """Tables with declared FK edges to both endpoint tables."""
        left_key, right_key = _norm(left), _norm(right)
        result: list[str] = []
        for table in self.tables:
            if _norm(table.name) in {left_key, right_key}:
                continue
            targets = {_norm(key.references_table) for key in table.foreign_keys}
            if {left_key, right_key}.issubset(targets):
                result.append(table.name)
        return tuple(sorted(result, key=_norm))

    def join_path(
        self, source: str, target: str, *, max_hops: int = 4
    ) -> tuple[str, ...] | None:
        """Find one deterministic undirected FK path, bounded to four hops."""
        max_hops = max(0, min(4, int(max_hops)))
        start, end = self.table(source), self.table(target)
        if start is None or end is None:
            return None
        graph: dict[str, set[str]] = {_norm(item.name): set() for item in self.tables}
        names = {_norm(item.name): item.name for item in self.tables}
        for table in self.tables:
            owner = _norm(table.name)
            for key in table.foreign_keys:
                referenced = _norm(key.references_table)
                if referenced in graph:
                    graph[owner].add(referenced)
                    graph[referenced].add(owner)
        start_key, end_key = _norm(start.name), _norm(end.name)
        queue = deque([(start_key, (start_key,))])
        seen = {start_key}
        while queue:
            node, path = queue.popleft()
            if node == end_key:
                return tuple(names[item] for item in path)
            if len(path) - 1 >= max_hops:
                continue
            for neighbor in sorted(graph[node]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append((neighbor, (*path, neighbor)))
        return None

    def public_facts(
        self,
        *,
        max_tables: int = 8,
        max_columns_per_table: int = 24,
    ) -> dict[str, Any]:
        """Return a learner-safe, independently bounded structural summary.

        The internal catalogue can retain more columns for diagnosis than a
        public response should serialize.  Bounds are clamped here so callers
        cannot accidentally turn a large (but valid) schema into an oversized
        learner payload.
        """
        try:
            table_limit = max(0, min(MAX_TABLES, int(max_tables)))
            column_limit = max(
                0,
                min(MAX_COLUMNS_PER_TABLE, int(max_columns_per_table)),
            )
        except (TypeError, ValueError, OverflowError):
            table_limit = 0
            column_limit = 0
        selected_tables = self.tables[:table_limit]
        visible_columns = {
            _norm(table.name): {
                _norm(column.name): column.name
                for column in table.columns[:column_limit]
            }
            for table in selected_tables
        }
        tables_out: list[dict[str, Any]] = []
        truncated = len(self.tables) > len(selected_tables)
        for table in selected_tables:
            columns = [
                item.public_fact() for item in table.columns[:column_limit]
            ]
            if len(table.columns) > len(columns):
                truncated = True
            local_columns = visible_columns.get(_norm(table.name), {})

            def local_names(names: Sequence[str]) -> list[str] | None:
                if len(names) > column_limit:
                    return None
                resolved = [local_columns.get(_norm(name)) for name in names]
                return list(resolved) if all(resolved) else None

            primary_key = local_names(table.primary_key) if table.primary_key else []
            if table.primary_key and primary_key is None:
                primary_key = []
                truncated = True

            unique_constraints: list[list[str]] = []
            unique_member_budget = column_limit
            raw_unique_constraints = table.unique_constraints[
                :MAX_CONSTRAINTS_PER_TABLE
            ]
            if len(table.unique_constraints) > len(raw_unique_constraints):
                truncated = True
            for constraint in raw_unique_constraints:
                resolved = local_names(constraint)
                if (
                    resolved is None
                    or len(resolved) > unique_member_budget
                    or len(unique_constraints) >= column_limit
                ):
                    truncated = True
                    continue
                unique_constraints.append(resolved)
                unique_member_budget -= len(resolved)

            foreign_keys: list[dict[str, Any]] = []
            foreign_member_budget = column_limit
            raw_foreign_keys = table.foreign_keys[:MAX_CONSTRAINTS_PER_TABLE]
            if len(table.foreign_keys) > len(raw_foreign_keys):
                truncated = True
            for key in raw_foreign_keys:
                source = local_names(key.columns)
                target_table = next(
                    (
                        item
                        for item in selected_tables
                        if _norm(item.name) == _norm(key.references_table)
                    ),
                    None,
                )
                target_lookup = visible_columns.get(_norm(key.references_table), {})
                target = [
                    target_lookup.get(_norm(name))
                    for name in key.references_columns
                ]
                if (
                    source is None
                    or target_table is None
                    or not all(target)
                    or len(source) != len(target)
                    or len(source) > foreign_member_budget
                    or len(foreign_keys) >= column_limit
                ):
                    truncated = True
                    continue
                foreign_keys.append({
                    "columns": source,
                    "references_table": target_table.name,
                    "references_columns": list(target),
                })
                foreign_member_budget -= len(source)

            fact = {
                "name": table.name,
                "confidence": table.confidence.value,
                "columns": columns,
                "primary_key": primary_key,
                "unique_constraints": unique_constraints,
                "foreign_keys": foreign_keys,
            }
            tables_out.append(fact)
        limitations = sorted({
            item
            if isinstance(item, str) and item in _PUBLIC_LIMITATION_CODES
            else "SCHEMA_METADATA_UNAVAILABLE"
            for item in self.limitations
        })
        if truncated and "PUBLIC_SCHEMA_SUMMARY_TRUNCATED" not in limitations:
            limitations.append("PUBLIC_SCHEMA_SUMMARY_TRUNCATED")
        result = {
            "confidence": self.confidence.value,
            "tables": tables_out,
            "limitations": limitations,
        }
        if len(
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ) > MAX_PUBLIC_OUTPUT_BYTES:
            # Constraint topology is the highest-amplification channel.  Drop
            # it before dropping table/column identities, and report the
            # degradation explicitly.
            for table in tables_out:
                table["primary_key"] = []
                table["unique_constraints"] = []
                table["foreign_keys"] = []
            if "PUBLIC_SCHEMA_SUMMARY_TRUNCATED" not in limitations:
                limitations.append("PUBLIC_SCHEMA_SUMMARY_TRUNCATED")
            while tables_out and len(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ) > MAX_PUBLIC_OUTPUT_BYTES:
                tables_out.pop()
        return result


def _spider_tables(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    names = _seq(payload.get("table_names_original") or payload.get("table_names"))
    raw_columns = _seq(payload.get("column_names_original") or payload.get("column_names"))
    if not names or not raw_columns:
        return [], None
    if len(names) > MAX_TABLES or len(raw_columns) > MAX_TOTAL_COLUMNS:
        return [], "SCHEMA_LIMIT_EXCEEDED"
    raw_primary_keys = _seq(payload.get("primary_keys"))
    raw_foreign_keys = _seq(payload.get("foreign_keys"))
    if (
        len(raw_primary_keys) > MAX_TOTAL_COLUMNS
        or len(raw_foreign_keys) > MAX_TOTAL_COLUMNS
    ):
        return [], "SCHEMA_LIMIT_EXCEEDED"
    tables = [{"name": name, "columns": [], "primary_key": [], "foreign_keys": []} for name in names]
    types = _seq(payload.get("column_types"))
    column_refs: dict[int, tuple[int, str]] = {}
    for index, raw in enumerate(raw_columns):
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        owner, name = raw[0], raw[1]
        if not isinstance(owner, int) or owner < 0 or owner >= len(tables) or name == "*":
            continue
        column = {"name": name}
        if index < len(types):
            column["data_type"] = types[index]
        tables[owner]["columns"].append(column)
        column_refs[index] = (owner, str(name))
    for raw_index in raw_primary_keys:
        if isinstance(raw_index, int) and raw_index in column_refs:
            owner, name = column_refs[raw_index]
            tables[owner]["primary_key"].append(name)
    for raw in raw_foreign_keys:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            continue
        source, target = column_refs.get(raw[0]), column_refs.get(raw[1])
        if source and target:
            tables[source[0]]["foreign_keys"].append({
                "column": source[1],
                "references_table": names[target[0]],
                "references_column": target[1],
            })
    for table in tables:
        if table["primary_key"]:
            table["unique_constraints"] = [list(table["primary_key"])]
    return tables, None


def _column_payload(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        return {"name": raw}
    return dict(raw) if isinstance(raw, Mapping) else None


def _names(value: Any, lookup: Mapping[str, str]) -> tuple[str, ...] | None:
    raw = _seq(value)
    if not raw or len(raw) > MAX_COLUMNS_PER_TABLE:
        return None
    resolved: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = lookup.get(_norm(str(item)))
        key = _norm(name)
        if name is None or key in seen:
            return None
        resolved.append(name)
        seen.add(key)
    return tuple(resolved)


def _unresolved_names(value: Any) -> tuple[str, ...] | None:
    raw = _seq(value)
    if not raw or len(raw) > MAX_COLUMNS_PER_TABLE:
        return None
    result: list[str] = []
    seen: set[str] = set()
    for raw_name in raw:
        name = _identifier(raw_name)
        key = _norm(name)
        if name is None or key in seen:
            return None
        result.append(name)
        seen.add(key)
    return tuple(result)


def _parse_table(raw: Mapping[str, Any]) -> tuple[TableFact | None, tuple[str, ...]]:
    limitations: list[str] = []
    name = _identifier(raw.get("name"), qualified=True)
    raw_columns = _seq(raw.get("columns"))
    if len(raw_columns) > MAX_COLUMNS_PER_TABLE:
        return None, ("SCHEMA_LIMIT_EXCEEDED",)
    if name is None or not raw_columns:
        return None, ("INVALID_TABLE_STRUCTURE",)
    columns_raw: dict[str, dict[str, Any]] = {}
    for item in raw_columns:
        payload = _column_payload(item)
        column_name = _identifier(payload.get("name")) if payload else None
        key = _norm(column_name)
        if not column_name or key in columns_raw:
            return None, ("INVALID_OR_DUPLICATE_COLUMN",)
        columns_raw[key] = payload
    lookup = {key: str(item["name"]).strip() for key, item in columns_raw.items()}

    inline_primary = [
        lookup[key] for key, item in columns_raw.items()
        if item.get("primary_key") is True or item.get("is_primary_key") is True
    ]
    primary_value = raw.get("primary_key")
    primary = _names(primary_value, lookup) if primary_value else tuple(inline_primary)
    if primary_value and primary is None:
        limitations.append("INVALID_PRIMARY_KEY_DROPPED")
        primary = ()
    primary = primary or ()

    uniques: list[tuple[str, ...]] = []
    raw_uniques = _seq(raw.get("unique_constraints"))
    if len(raw_uniques) > MAX_CONSTRAINTS_PER_TABLE:
        return None, ("SCHEMA_LIMIT_EXCEEDED",)
    for item in raw_uniques:
        resolved = _names(item, lookup)
        if resolved and resolved not in uniques:
            uniques.append(resolved)
        else:
            limitations.append("INVALID_UNIQUE_CONSTRAINT_DROPPED")
    for key, item in columns_raw.items():
        if item.get("unique") is True or item.get("is_unique") is True:
            unary = (lookup[key],)
            if unary not in uniques:
                uniques.append(unary)
    if primary and primary not in uniques:
        uniques.append(primary)

    primary_keys = {_norm(item) for item in primary}
    unique_unary = {_norm(item[0]) for item in uniques if len(item) == 1}
    columns: list[ColumnFact] = []
    declared = bool(primary or uniques)
    for key, item in columns_raw.items():
        dtype_raw = item.get("data_type", item.get("type"))
        dtype = _data_type(dtype_raw)
        if dtype_raw is not None and dtype is None:
            limitations.append("INVALID_COLUMN_TYPE_DROPPED")
        nullable = _bool_or_none(item.get("nullable"))
        if nullable is None and isinstance(item.get("not_null"), bool):
            nullable = not item["not_null"]
        if key in primary_keys:
            nullable = False
        declared |= dtype is not None or nullable is not None
        columns.append(ColumnFact(
            name=lookup[key],
            data_type=dtype,
            nullable=nullable,
            primary_key=key in primary_keys,
            unique=key in unique_unary,
        ))

    foreign_keys: list[ForeignKeyFact] = []
    raw_fks = _seq(raw.get("foreign_keys"))
    if len(raw_fks) > MAX_CONSTRAINTS_PER_TABLE:
        return None, ("SCHEMA_LIMIT_EXCEEDED",)
    for item in raw_fks:
        if not isinstance(item, Mapping):
            limitations.append("INVALID_FOREIGN_KEY_DROPPED")
            continue
        source_raw = item.get("columns") or ([item.get("column")] if item.get("column") else ())
        target_raw = item.get("references_columns") or (
            [item.get("references_column")] if item.get("references_column") else ()
        )
        source = _names(source_raw, lookup)
        target = _unresolved_names(target_raw)
        target_table = _identifier(
            item.get("references_table") or item.get("reference_table"),
            qualified=True,
        )
        if not source or not target or len(source) != len(target) or not target_table:
            limitations.append("INVALID_FOREIGN_KEY_DROPPED")
            continue
        foreign_keys.append(ForeignKeyFact(source, target_table, target))
        declared = True

    return TableFact(
        name=name,
        columns=tuple(sorted(columns, key=lambda item: _norm(item.name))),
        primary_key=primary,
        foreign_keys=tuple(sorted(
            set(foreign_keys),
            key=lambda item: (_norm(item.references_table), item.columns),
        )),
        unique_constraints=tuple(sorted(set(uniques))),
        confidence=(SchemaConfidence.DECLARED if declared else SchemaConfidence.STRUCTURE_ONLY),
    ), tuple(sorted(set(limitations)))


def parse_schema_catalog(value: Any) -> Phase2SchemaCatalog:
    """Parse a bounded JSON/dict catalog without raising on untrusted input."""
    if isinstance(value, str):
        if len(value) > MAX_INPUT_BYTES or len(
            value.encode("utf-8", errors="ignore")
        ) > MAX_INPUT_BYTES:
            return Phase2SchemaCatalog.unknown("SCHEMA_INPUT_TOO_LARGE")
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return Phase2SchemaCatalog.unknown("SCHEMA_JSON_INVALID")
    if not isinstance(value, Mapping):
        return Phase2SchemaCatalog.unknown("SCHEMA_INPUT_UNSUPPORTED")
    nested = value.get("schema")
    if isinstance(nested, Mapping) and "tables" not in value:
        value = nested

    raw_tables = _seq(value.get("tables"))
    if not raw_tables:
        raw_tables, spider_error = _spider_tables(value)
        if spider_error:
            return Phase2SchemaCatalog.unknown(spider_error)
    if not raw_tables:
        compact_items = list(islice(value.items(), MAX_TABLES + 1))
        compact = [
            {"name": name, "columns": columns}
            for name, columns in compact_items
            if _identifier(name, qualified=True) and isinstance(columns, (list, tuple))
        ]
        if len(compact_items) > MAX_TABLES or len(compact) > MAX_TABLES:
            return Phase2SchemaCatalog.unknown("SCHEMA_LIMIT_EXCEEDED")
        raw_tables = compact
    if not raw_tables:
        return Phase2SchemaCatalog.unknown("SCHEMA_TABLES_MISSING")
    if len(raw_tables) > MAX_TABLES:
        return Phase2SchemaCatalog.unknown("SCHEMA_LIMIT_EXCEEDED")

    parsed: list[TableFact] = []
    limitations: list[str] = []
    total_columns = 0
    seen: set[str] = set()
    for raw in raw_tables:
        if not isinstance(raw, Mapping):
            limitations.append("INVALID_TABLE_DROPPED")
            continue
        table, table_limits = _parse_table(raw)
        limitations.extend(table_limits)
        if "SCHEMA_LIMIT_EXCEEDED" in table_limits:
            return Phase2SchemaCatalog.unknown("SCHEMA_LIMIT_EXCEEDED")
        if table is None:
            continue
        key = _norm(table.name)
        if key in seen:
            return Phase2SchemaCatalog.unknown("DUPLICATE_TABLE")
        seen.add(key)
        total_columns += len(table.columns)
        if total_columns > MAX_TOTAL_COLUMNS:
            return Phase2SchemaCatalog.unknown("SCHEMA_LIMIT_EXCEEDED")
        parsed.append(table)
    if not parsed:
        return Phase2SchemaCatalog.unknown(
            limitations[0] if limitations else "SCHEMA_TABLES_UNUSABLE"
        )

    # Validate FK targets only after every table/column has been normalized.
    table_lookup = {_norm(item.name): item for item in parsed}
    validated: list[TableFact] = []
    for table in parsed:
        keys: list[ForeignKeyFact] = []
        seen_keys: set[tuple[Any, ...]] = set()
        for key in table.foreign_keys:
            target = table_lookup.get(_norm(key.references_table))
            if target is None or any(target.column(name) is None for name in key.references_columns):
                limitations.append("UNRESOLVED_FOREIGN_KEY_DROPPED")
                continue
            normalized_key = ForeignKeyFact(
                key.columns,
                target.name,
                tuple(target.column(name).name for name in key.references_columns if target.column(name)),
            )
            identity = (
                tuple(_norm(name) for name in normalized_key.columns),
                _norm(normalized_key.references_table),
                tuple(_norm(name) for name in normalized_key.references_columns),
            )
            if identity not in seen_keys:
                keys.append(normalized_key)
                seen_keys.add(identity)
        has_declaration = bool(
            table.primary_key
            or table.unique_constraints
            or keys
            or any(
                column.data_type is not None or column.nullable is not None
                for column in table.columns
            )
        )
        validated.append(TableFact(
            name=table.name,
            columns=table.columns,
            primary_key=table.primary_key,
            foreign_keys=tuple(keys),
            unique_constraints=table.unique_constraints,
            confidence=(
                SchemaConfidence.DECLARED
                if has_declaration
                else SchemaConfidence.STRUCTURE_ONLY
            ),
        ))
    validated.sort(key=lambda item: _norm(item.name))
    confidence = (
        SchemaConfidence.DECLARED
        if any(item.confidence is SchemaConfidence.DECLARED for item in validated)
        else SchemaConfidence.STRUCTURE_ONLY
    )
    if confidence is SchemaConfidence.STRUCTURE_ONLY:
        limitations.append("CONSTRAINTS_AND_TYPES_UNKNOWN")
    return Phase2SchemaCatalog(
        tables=tuple(validated),
        confidence=confidence,
        limitations=tuple(sorted(set(limitations))),
    )


__all__ = [
    "ColumnFact",
    "ForeignKeyFact",
    "JoinCardinality",
    "MAX_COLUMNS_PER_TABLE",
    "MAX_INPUT_BYTES",
    "MAX_PUBLIC_OUTPUT_BYTES",
    "MAX_TABLES",
    "Phase2SchemaCatalog",
    "SchemaConfidence",
    "TableFact",
    "parse_schema_catalog",
]
