"""Small, independent execution oracle for Phase 1 corpus audits.

This module intentionally does not import the Phase 1 witness generator,
planner, validators, or mutation code.  It creates a bounded SQLite world from
the supplied schema, runs both queries against the same world, and reports
whether the observed result differs.  Matching finite executions are never
treated as a proof for an unlabelled pair: the public verdict is UNDECIDED
unless a trusted ``expected=equivalent`` label is supplied.

The oracle is a development oracle for SQLite-compatible teaching SQL.  A
declared vendor dialect is an ENGINE_GAP until a native runner is explicitly
connected.  All limits are deliberate so malformed recursive queries or
Cartesian products cannot consume unbounded memory.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import lru_cache
import hashlib
import json
import os
import random
import re
import sqlite3
import socket
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit


EQUIVALENT = "EQUIVALENT"
NOT_EQUIVALENT = "NOT_EQUIVALENT"
UNDECIDED = "UNDECIDED"
ENGINE_GAP = "ENGINE_GAP"
INPUT_GAP = "INPUT_GAP"

SUPPORTED_DIALECTS = {None, "", "generic", "standard", "sqlite"}
# Vendor dialects the oracle can run against a native engine once a URL is
# configured.  Without the URL they stay ``ENGINE_GAP`` so a declared dialect
# is never silently re-validated through SQLite.
NATIVE_DIALECTS = {
    "mysql": ("mysql", "PARSEVAL_MYSQL_URL"),
    "mariadb": ("mysql", "PARSEVAL_MYSQL_URL"),
    "postgres": ("postgres", "PARSEVAL_POSTGRES_URL"),
    "postgresql": ("postgres", "PARSEVAL_POSTGRES_URL"),
    "pg": ("postgres", "PARSEVAL_POSTGRES_URL"),
    "tsql": ("tsql", "PARSEVAL_TSQL_URL"),
    "mssql": ("tsql", "PARSEVAL_TSQL_URL"),
    "sqlserver": ("tsql", "PARSEVAL_TSQL_URL"),
    "sql_server": ("tsql", "PARSEVAL_TSQL_URL"),
    "oracle": ("oracle", "PARSEVAL_ORACLE_URL"),
    "ora": ("oracle", "PARSEVAL_ORACLE_URL"),
}
MYSQL_TARGET_VERSION = "8.0.46"
MYSQL_REQUIRED_LOWER_CASE_TABLE_NAMES = 0
MYSQL_FIXTURE_IDENTIFIER_POLICY = "preserve_source_spelling"
MAX_ROWS_PER_TABLE = 32
MAX_RESULT_ROWS = 256
MAX_VM_STEPS = 250_000


def _read_only_authorizer(
    action_code: int,
    _arg1: str | None,
    _arg2: str | None,
    _database: str | None,
    _source: str | None,
) -> int:
    """Allow result reads only after the fixture has been materialized."""
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    return sqlite3.SQLITE_OK if action_code in allowed else sqlite3.SQLITE_DENY


@dataclass(frozen=True)
class ColumnDef:
    name: str
    declared_type: str = "TEXT"
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    # True when the source actually stated a type.  Compact corpus schemas such
    # as ``matches(surface, score)`` state none, so the type is guessed and may
    # be refined from how the queries use the column.
    type_declared: bool = False


@dataclass(frozen=True)
class ForeignKeyDef:
    columns: tuple[str, ...]
    references_table: str
    references_columns: tuple[str, ...]


@dataclass
class TableDef:
    name: str
    columns: list[ColumnDef] = field(default_factory=list)
    primary_key: tuple[str, ...] = ()
    unique_constraints: list[tuple[str, ...]] = field(default_factory=list)
    foreign_keys: list[ForeignKeyDef] = field(default_factory=list)


class OracleInputError(ValueError):
    """The schema or query pair cannot be used as an oracle input."""


class OracleEngineGap(RuntimeError):
    """The bounded SQLite runner cannot execute the requested feature."""


def _native_error_code(exc: BaseException) -> str | int | None:
    """Extract only a structured DB-API error code, never connection text."""
    current: BaseException | None = exc
    while current is not None:
        for attribute in ("errno", "sqlstate", "pgcode", "code"):
            value = getattr(current, attribute, None)
            if isinstance(value, (int, str)) and str(value).strip():
                return value
        for argument in getattr(current, "args", ()):
            if isinstance(argument, int):
                return argument
            if isinstance(argument, str):
                match = re.fullmatch(r"\s*(\d{3,5}|[0-9A-Z]{5})\s*", argument, re.IGNORECASE)
                if match:
                    return match.group(1).upper()
        current = current.__cause__
    return None


def _native_schema_resolution_kind(exc: BaseException, dialect: str | None) -> str | None:
    """Classify a physical schema-name error without treating the engine as absent."""
    normalized = _norm(dialect)
    engine = NATIVE_DIALECTS.get(normalized, (normalized, ""))[0]
    code = str(_native_error_code(exc) or "").upper()
    text = str(exc).lower()
    if engine == "mysql":
        if code in {"1146", "1051"} or re.search(r"table .*doesn.?t exist|unknown table", text):
            return "mysql.table_not_found"
        if code == "1054" or "unknown column" in text:
            return "mysql.column_not_found"
    elif engine == "postgres":
        if code == "42P01" or re.search(r"relation .*does not exist", text):
            return "postgres.table_not_found"
        if code == "42703" or re.search(r"column .*does not exist", text):
            return "postgres.column_not_found"
    elif engine == "tsql":
        if code == "208" or "invalid object name" in text:
            return "tsql.table_not_found"
        if code == "207" or "invalid column name" in text:
            return "tsql.column_not_found"
    elif engine == "oracle":
        if code in {"ORA-00942", "942"} or "ora-00942" in text:
            return "oracle.table_not_found"
        if code in {"ORA-00904", "904"} or "ora-00904" in text:
            return "oracle.column_not_found"
    if isinstance(exc, sqlite3.DatabaseError):
        if "no such table" in text:
            return "sqlite.table_not_found"
        if "no such column" in text:
            return "sqlite.column_not_found"
    return None


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _unquote_identifier(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "`\"[]":
        if value[0] == "[":
            return value[1:-1].replace("]]", "]")
        return value[1:-1].replace(value[0] * 2, value[0])
    return value


def _quote_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text or "." in text:
        raise OracleInputError(f"unsafe table or column identifier: {value!r}")
    return '"' + text.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Native engine connections
#
# SQLite is always available.  MySQL and PostgreSQL are optional native
# engines, wired through ``PARSEVAL_MYSQL_URL`` / ``PARSEVAL_POSTGRES_URL`` so
# the oracle stays a pure read-only execution oracle (no Docker socket is ever
# handed to the process).  A declared vendor dialect with no reachable URL is
# an ``ENGINE_GAP`` rather than a silent SQLite fallback, so the teaching pair
# is honestly reported as out-of-scope instead of mis-validated.
# ---------------------------------------------------------------------------


def _env_url(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    # The audit is commonly launched as a standalone script. Read only the
    # four native URL keys from the local development env when the caller did
    # not export them; never print or copy the values into audit artifacts.
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "sql-edu-backend", ".env"))
    try:
        for line in open(env_path, encoding="utf-8"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, candidate = stripped.split("=", 1)
            if key.strip() == name:
                candidate = candidate.strip().strip('\"\'')
                return candidate or None
    except (OSError, UnicodeError):
        pass
    return None


@lru_cache(maxsize=None)
def _native_dialect_url(dialect: str) -> str | None:
    """Return a configured *reachable* native engine URL for a dialect.

    SQLite-family dialects (generic/standard/sqlite) have no URL: they run
    in-process.  A vendor dialect with no URL, an unusable URL, or a closed
    endpoint is an ``ENGINE_GAP``.  The small TCP probe prevents a stale local
    ``.env`` entry from making unit tests (and public audits) claim that a
    native backend is available when the service is not running.  The probe is
    cached per imported oracle module, so a large audit does not perform a
    network operation per row.
    """
    normalized = _norm(dialect)
    entry = NATIVE_DIALECTS.get(normalized)
    if entry is None:
        return None
    _engine, env_name = entry
    url = _env_url(env_name)
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        # A driver-specific socket/DSN (for example a local Unix socket) is
        # left to the native runner; only probe ordinary TCP URLs here.
        if not host or not port:
            return url
        with socket.create_connection((host, port), timeout=0.15):
            return url
    except (OSError, ValueError):
        return None


def _split_top_level(text: str, delimiter: str = ",") -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif quote == "]" and char == "]":
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise OracleInputError("unbalanced schema parentheses")
        elif char == delimiter and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    if quote or depth != 0:
        raise OracleInputError("unterminated schema quote or parentheses")
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _split_table_specs(schema: str) -> list[str]:
    """Split compact ``table(columns); table(columns)`` schema safely."""
    specs: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    for index, char in enumerate(schema):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise OracleInputError("unbalanced schema parentheses")
        elif char == ";" and depth == 0:
            if schema[start:index].strip():
                specs.append(schema[start:index].strip())
            start = index + 1
    if quote or depth != 0:
        raise OracleInputError("unbalanced schema definition")
    if schema[start:].strip():
        specs.append(schema[start:].strip())
    return specs


def _parse_column_definition(definition: str) -> ColumnDef:
    match = re.match(
        # A bare digit-leading name is accepted because scraped table headers
        # legitimately contain year columns such as ``2006``; SQLite receives it
        # quoted, so it stays a valid identifier.
        r"(?is)^\s*(`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|\w[\w$]*)\s*(.*)$",
        definition,
    )
    if not match:
        raise OracleInputError(f"invalid column definition: {definition!r}")
    name = _unquote_identifier(match.group(1))
    tail = match.group(2).strip()
    constraint_match = re.search(r"(?is)\b(?:primary\s+key|unique|not\s+null)\b", tail)
    declared_type = (tail[: constraint_match.start()] if constraint_match else tail).strip()
    stated_type = declared_type
    if not declared_type:
        declared_type = (
            "INTEGER"
            if re.search(
                r"(?i)(?:^|_)(?:id|no|year|salary|credits?|amount|score|price|count|qty|quantity|budget|total)(?:$|_)|(?:^|_)id$",
                name,
            )
            else "TEXT"
        )
    upper = tail.upper()
    primary = "PRIMARY KEY" in upper
    return ColumnDef(
        name=name,
        declared_type=declared_type,
        nullable=not ("NOT NULL" in upper or primary),
        primary_key=primary,
        unique="UNIQUE" in upper,
        type_declared=bool(stated_type),
    )


def _parse_compact_schema(schema: str) -> list[TableDef]:
    tables: list[TableDef] = []
    for spec in _split_table_specs(schema):
        match = re.match(
            r"(?is)^\s*(`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|\w[\w$]*)\s*\((.*)\)\s*$",
            spec,
        )
        if not match:
            raise OracleInputError(f"invalid table definition: {spec!r}")
        table = TableDef(_unquote_identifier(match.group(1)))
        table_constraints: list[str] = []
        for item in _split_top_level(match.group(2)):
            if re.match(r"(?is)^\s*(?:primary\s+key|foreign\s+key|unique)\b", item):
                table_constraints.append(item)
            else:
                table.columns.append(_parse_column_definition(item))
        inline_pk = tuple(column.name for column in table.columns if column.primary_key)
        if inline_pk:
            table.primary_key = inline_pk
        for constraint in table_constraints:
            upper = constraint.upper()
            pk_match = re.search(r"(?is)PRIMARY\s+KEY\s*\(([^)]*)\)", constraint)
            if pk_match:
                table.primary_key = tuple(
                    _unquote_identifier(item) for item in _split_top_level(pk_match.group(1))
                )
                continue
            unique_match = re.search(r"(?is)UNIQUE\s*\(([^)]*)\)", constraint)
            if unique_match:
                table.unique_constraints.append(tuple(
                    _unquote_identifier(item) for item in _split_top_level(unique_match.group(1))
                ))
                continue
            fk_match = re.search(
                r"(?is)FOREIGN\s+KEY\s*\(([^)]*)\)\s+REFERENCES\s+(`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$]*)\s*\(([^)]*)\)",
                constraint,
            )
            if fk_match:
                table.foreign_keys.append(ForeignKeyDef(
                    tuple(_unquote_identifier(item) for item in _split_top_level(fk_match.group(1))),
                    _unquote_identifier(fk_match.group(2)),
                    tuple(_unquote_identifier(item) for item in _split_top_level(fk_match.group(3))),
                ))
        if not table.name or not table.columns:
            raise OracleInputError("schema table must contain a name and at least one column")
        tables.append(table)
    if not tables:
        raise OracleInputError("schema contains no tables")
    return tables


def _catalog_tables(schema_catalog: Any) -> list[TableDef]:
    if not isinstance(schema_catalog, dict) or not isinstance(schema_catalog.get("tables"), list):
        raise OracleInputError("schema catalog contains no physical tables")
    result: list[TableDef] = []
    for raw_table in schema_catalog["tables"]:
        if not isinstance(raw_table, dict):
            continue
        table = TableDef(name=str(raw_table.get("name") or "").strip())
        table.primary_key = tuple(str(value) for value in raw_table.get("primary_key") or ())
        table.unique_constraints = [
            tuple(str(value) for value in values)
            for values in raw_table.get("unique_constraints") or ()
            if isinstance(values, (list, tuple)) and values
        ]
        for raw_column in raw_table.get("columns") or ():
            if not isinstance(raw_column, dict):
                continue
            name = str(raw_column.get("name") or "").strip()
            if not name:
                continue
            primary = bool(raw_column.get("is_primary_key")) or name in table.primary_key
            nullable = raw_column.get("nullable")
            table.columns.append(ColumnDef(
                name=name,
                declared_type=str(raw_column.get("data_type") or "TEXT"),
                nullable=(True if nullable is None else bool(nullable)) and not primary,
                primary_key=primary,
                unique=False,
                type_declared=bool(raw_column.get("data_type")),
            ))
        for raw_fk in raw_table.get("foreign_keys") or ():
            if not isinstance(raw_fk, dict):
                continue
            source = str(raw_fk.get("column") or "").strip()
            target_table = str(raw_fk.get("references_table") or "").strip()
            target_column = str(raw_fk.get("references_column") or "").strip()
            if source and target_table and target_column:
                table.foreign_keys.append(ForeignKeyDef((source,), target_table, (target_column,)))
        if table.name and table.columns:
            result.append(table)
    if not result:
        raise OracleInputError("schema catalog contains no usable tables")
    return result


def _strip_schema_comments(schema: str) -> str:
    """Drop SQL comments before parsing a compact schema.

    Several corpus sources prefix the schema with provenance comments such as
    ``-- spider_db_id: college_2``.  Without this pass the comment is glued onto
    the first table name and the whole record is rejected as INPUT_GAP, which
    understates the schema recognition rate the acceptance plan measures.
    """
    text = re.sub(r"/\*.*?\*/", " ", schema, flags=re.DOTALL)
    cleaned: list[str] = []
    for line in text.splitlines():
        quote: str | None = None
        cut = len(line)
        index = 0
        while index < len(line):
            char = line[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in "'\"`":
                quote = char
            elif char == "[":
                quote = "]"
            elif char == "-" and line.startswith("--", index):
                cut = index
                break
            index += 1
        cleaned.append(line[:cut])
    return "\n".join(cleaned)


def parse_schema(schema: str | None, schema_catalog: Any = None) -> list[TableDef]:
    if schema_catalog is not None:
        return _catalog_tables(schema_catalog)
    if not isinstance(schema, str) or not schema.strip():
        raise OracleInputError("schema is required")
    return _parse_compact_schema(_strip_schema_comments(schema))


def _sqlite_type(declared_type: str) -> str:
    upper = declared_type.upper()
    if any(token in upper for token in ("INT", "BOOL")):
        return "INTEGER"
    if any(token in upper for token in ("REAL", "FLOA", "DOUB", "DEC", "NUM")):
        return "REAL"
    if any(token in upper for token in ("BLOB",)):
        return "BLOB"
    return "TEXT"


def _temporal_kind(declared_type: str) -> str | None:
    upper = str(declared_type or "").upper()
    if "TIMESTAMP" in upper or "DATETIME" in upper:
        return "timestamp"
    if re.search(r"\bDATE\b", upper):
        return "date"
    if re.search(r"\bTIME\b", upper):
        return "time"
    return None


def _declared_text_limit(declared_type: str) -> int | None:
    match = re.search(r"(?is)\b(?:N?VARCHAR|N?CHAR|VARCHAR2)\s*\(\s*(\d+)\s*(?:CHAR|BYTE)?\s*\)", str(declared_type or ""))
    return int(match.group(1)) if match else None


def _create_table_sql(table: TableDef) -> str:
    definitions: list[str] = []
    primary = set(table.primary_key)
    for column in table.columns:
        definition = f"{_quote_identifier(column.name)} {_sqlite_type(column.declared_type)}"
        if column.primary_key and len(table.primary_key) <= 1:
            definition += " PRIMARY KEY"
        if column.unique:
            definition += " UNIQUE"
        if not column.nullable:
            definition += " NOT NULL"
        definitions.append(definition)
    if len(table.primary_key) > 1:
        definitions.append("PRIMARY KEY (" + ", ".join(_quote_identifier(item) for item in table.primary_key) + ")")
    for unique in table.unique_constraints:
        definitions.append("UNIQUE (" + ", ".join(_quote_identifier(item) for item in unique) + ")")
    for foreign_key in table.foreign_keys:
        definitions.append(
            "FOREIGN KEY (" + ", ".join(_quote_identifier(item) for item in foreign_key.columns) + ") "
            "REFERENCES " + _quote_identifier(foreign_key.references_table) + " ("
            + ", ".join(_quote_identifier(item) for item in foreign_key.references_columns) + ")"
        )
    return f"CREATE TABLE {_quote_identifier(table.name)} (" + ", ".join(definitions) + ")"


def _value_for(column: ColumnDef, row_index: int, rng: random.Random, *, force_non_null: bool = False) -> Any:
    column_name = _norm(column.name)
    temporal = _temporal_kind(column.declared_type)
    if temporal:
        if column.nullable and not force_non_null and row_index == 2:
            return None
        if temporal == "date":
            return ("2020-01-01", "2024-02-29", "2021-06-15", "2023-12-31")[row_index % 4]
        if temporal == "timestamp":
            return (
                "2020-01-01 00:00:00",
                "2024-02-29 12:30:00",
                "2021-06-15 08:00:00",
                "2023-12-31 23:59:59",
            )[row_index % 4]
        return ("00:00:00", "12:30:00", "08:00:00", "23:59:59")[row_index % 4]
    kind = _sqlite_type(column.declared_type)
    if kind == "TEXT" and re.search(
        r"(?i)(?:^|_)(?:id|no|year|salary|credits?|amount|score|price|count|qty|quantity|budget|total)(?:$|_)|(?:^|_)id$",
        column_name,
    ):
        kind = "INTEGER"
    if column.primary_key or column.unique:
        if kind == "INTEGER":
            return row_index + 1
        return f"{column.name}_{row_index + 1}"
    # Reserve the first few slots for teaching boundaries.  Random values are
    # still used after these sentinels, but a single seed/scale must not miss
    # the canonical ``> c`` versus ``>= c`` witness by accident.
    if kind in {"INTEGER", "REAL"}:
        if row_index == 0:
            return 1 if kind == "INTEGER" else 1.0
        if row_index == 1:
            return 3 if kind == "INTEGER" else 3.0
        if column.nullable and not force_non_null and row_index == 2:
            return None
        if row_index == 3:
            return 5 if kind == "INTEGER" else 5.0
    if column.nullable and not force_non_null and rng.random() < 0.14:
        return None
    if kind == "INTEGER":
        return rng.choice([-2, -1, 0, 1, 2, 3, 4, 5, 6, 10, 18, 30])
    if kind == "REAL":
        return rng.choice([-1.5, 0.0, 1.5, 3.0, 5.0, 10.0, 18.5])
    if kind == "BLOB":
        return f"blob_{row_index}".encode("utf-8")
    if column_name in {"grade", "letter_grade", "level"}:
        return ("A", "A", "B", "C")[row_index] if row_index < 4 else rng.choice(("A", "B", "C"))
    if column_name in {"dept", "department", "department_name", "category", "region", "group_id"}:
        return ("Engineering", "Engineering", "Sales", "Sales")[row_index] if row_index < 4 else rng.choice(("Engineering", "Sales"))
    if column_name in {"name", "title", "label"}:
        return ("DataX", "XData", "Alice", "Carol")[row_index] if row_index < 4 else rng.choice(("Alice", "Bob", "Alice", "Carol"))
    generic = [
        "", "A", "B", "Data", "Data%", "alice", "bob", "Engineering", "Sales",
        "2020-01-01", "2024-02-29", "x_y",
    ]
    # The first rows are pinned to distinct values so MIN/MAX, ORDER BY and
    # DISTINCT over a text column always have something to separate; random
    # choices only fill the tail.
    if row_index < 4:
        return generic[row_index]
    return rng.choice(generic)


def _generate_rows(
    tables: Sequence[TableDef],
    row_count: int,
    seed: int,
    *,
    duplicate_rows: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(seed)
    rows: dict[str, list[dict[str, Any]]] = {}
    for table in tables:
        table_rows: list[dict[str, Any]] = []
        for index in range(row_count):
            row = {
                column.name: _value_for(column, index, rng, force_non_null=column.primary_key)
                for column in table.columns
            }
            table_rows.append(row)
        if duplicate_rows:
            _duplicate_pairs(table, table_rows)
        rows[table.name] = table_rows
    return rows


def _duplicate_pairs(table: TableDef, table_rows: list[dict[str, Any]]) -> None:
    """Make adjacent rows agree on every non-key column.

    Without a world that actually contains duplicate rows, DISTINCT, UNION ALL
    and bag-semantics mutations return the same result as their gold query and
    are reported UNDECIDED instead of being distinguished.  Key columns keep
    their generated values so uniqueness constraints still load.
    """
    protected = _protected_columns(table)
    for index in range(1, len(table_rows), 2):
        source = table_rows[index - 1]
        target = table_rows[index]
        for column in table.columns:
            if column.name in protected:
                continue
            target[column.name] = source[column.name]


def _protected_columns(table: TableDef) -> set[str]:
    protected = {column.name for column in table.columns if column.primary_key or column.unique}
    protected.update(table.primary_key or ())
    for constraint in table.unique_constraints:
        protected.update(constraint)
    return protected


def _apply_query_boundaries(
    rows: dict[str, list[dict[str, Any]]],
    tables: Sequence[TableDef],
    queries: Sequence[str],
) -> None:
    """Materialize small comparison and aggregate boundaries deterministically."""
    table_by_column: dict[str, tuple[str, str]] = {}
    for table in tables:
        for column in table.columns:
            table_by_column.setdefault(_norm(column.name), (table.name, column.name))
    for sql in queries:
        for match in re.finditer(
            r"(?is)\bABS\s*\(\s*(?P<column>[A-Za-z_][A-Za-z0-9_$]*)\s*\)",
            sql,
        ):
            target = table_by_column.get(_norm(match.group("column")))
            if target is not None and rows.get(target[0]):
                rows[target[0]][-1][target[1]] = -5
        for window in re.finditer(
            r"(?is)\bPARTITION\s+BY\s+(?P<partition>[A-Za-z_][A-Za-z0-9_$]*)\s+"
            r"ORDER\s+BY\s+(?P<order>[A-Za-z_][A-Za-z0-9_$]*)",
            sql,
        ):
            partition_target = table_by_column.get(_norm(window.group("partition")))
            order_target = table_by_column.get(_norm(window.group("order")))
            if partition_target is None or order_target is None:
                continue
            if partition_target[0] != order_target[0]:
                continue
            table_rows = rows.get(partition_target[0]) or []
            if len(table_rows) >= 2:
                table_rows[0][partition_target[1]] = "Engineering"
                table_rows[1][partition_target[1]] = "Engineering"
                table_rows[0][order_target[1]] = 10
                table_rows[1][order_target[1]] = 10
        for match in re.finditer(
            r"(?is)(?P<column>[A-Za-z_][A-Za-z0-9_$]*)\s*(?P<operator>>=|<=|<>|!=|=|>|<)\s*(?P<value>-?\d+(?:\.\d+)?)",
            sql,
        ):
            target = table_by_column.get(_norm(match.group("column")))
            if target is None:
                continue
            table_name, column_name = target
            table_rows = rows.get(table_name) or []
            if table_rows:
                value = float(match.group("value")) if "." in match.group("value") else int(match.group("value"))
                table_rows[0][column_name] = value
        for match in re.finditer(
            r"(?is)\b(?:SUM|AVG|MIN|MAX|COUNT)\s*\(\s*(?P<column>[A-Za-z_][A-Za-z0-9_$]*)\s*\)\s*"
            r"(?P<operator>>=|<=|=|>|<)\s*(?P<value>-?\d+(?:\.\d+)?)",
            sql,
        ):
            target = table_by_column.get(_norm(match.group("column")))
            if target is None:
                continue
            table_name, column_name = target
            table_rows = rows.get(table_name) or []
            if len(table_rows) < 2:
                continue
            raw = float(match.group("value")) if "." in match.group("value") else int(match.group("value"))
            if match.group(0).upper().lstrip().startswith("COUNT"):
                continue
            half = raw / 2
            if isinstance(raw, int):
                half = raw // 2
                remainder = raw - half
            else:
                remainder = half
            table_rows[0][column_name] = half
            table_rows[1][column_name] = remainder


# SQL teaching corpora contain Unicode headers (for example ``rōmaji_title``
# and ``nº``) as well as numeric-leading WikiTable headers.  Keep the quoted
# forms, but also accept Unicode word identifiers and numeric-leading names so
# literal seeding and COUNT NULL paths can reach the same rows that the parser
# accepts.  This is a lexical boundary only; schema resolution remains the
# source of truth for whether a name actually exists.
_IDENTIFIER = (
    r"(?:[A-Za-z_][A-Za-z0-9_$]*|[^\W\d]\w*|\d[\w$]*|"
    r"\"[^\"]+\"|`[^`]+`|\[[^\]]+\])"
)
_LITERAL = r"'(?:[^']|'')*'|\"[^\"]*\"|-?\d+(?:\.\d+)?"

_LITERAL_PREDICATE = re.compile(
    rf"(?is)(?:(?:{_IDENTIFIER})\s*\.\s*)?(?P<column>{_IDENTIFIER})"
    rf"\s*(?P<operator><>|!=|>=|<=|=|>|<)\s*(?P<value>{_LITERAL})"
)

_LITERAL_MEMBERSHIP = re.compile(
    rf"(?is)(?:(?:{_IDENTIFIER})\s*\.\s*)?(?P<column>{_IDENTIFIER})"
    rf"\s+(?:NOT\s+)?IN\s*\(\s*(?P<values>(?:{_LITERAL})(?:\s*,\s*(?:{_LITERAL}))*)\s*\)"
)

_LITERAL_PATTERN = re.compile(
    rf"(?is)(?:(?:{_IDENTIFIER})\s*\.\s*)?(?P<column>{_IDENTIFIER})"
    rf"\s+(?:NOT\s+)?LIKE\s+(?P<value>'(?:[^']|'')*'|\"[^\"]*\")"
)

_SUBQUERY_MEMBERSHIP = re.compile(
    rf"(?is)(?P<outer_qual>(?:{_IDENTIFIER})\s*\.\s*)?"
    rf"(?P<outer_column>{_IDENTIFIER})\s+IN\s*\(\s*SELECT\s+"
    rf"(?:DISTINCT\s+)?(?P<inner_qual>(?:{_IDENTIFIER})\s*\.\s*)?"
    rf"(?P<inner_column>{_IDENTIFIER})\s+FROM\s+(?P<inner_table>{_IDENTIFIER})"
)

_JOIN_EQUALITY = re.compile(
    rf"(?is)\bON\b[^()]*?(?:(?P<left_table>{_IDENTIFIER})\s*\.\s*)?(?P<left>{_IDENTIFIER})"
    rf"\s*=\s*(?:(?P<right_table>{_IDENTIFIER})\s*\.\s*)?(?P<right>{_IDENTIFIER})"
)

_UNMATCHED_TEXT = "__gold_oracle_unmatched__"
_UNMATCHED_NUMBER = 987654321


def _literal_value(token: str) -> Any:
    token = token.strip()
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    if token.startswith('"'):
        return token[1:-1]
    return float(token) if "." in token else int(token)


def _pattern_witness(token: str) -> str | None:
    """A value that satisfies the given LIKE pattern, or None."""
    raw = _literal_value(token)
    if not isinstance(raw, str):
        return None
    core = raw.replace("%", "").replace("_", "x")
    return core or None


def _apply_query_literals(
    rows: dict[str, list[dict[str, Any]]],
    tables: Sequence[TableDef],
    queries: Sequence[str],
    *,
    layout: str = "sliding",
) -> None:
    """Plant the constants the queries compare against into the generated rows.

    Random data almost never satisfies a predicate such as ``surface = 'clay'``,
    so both queries return nothing and even a blunt AND/OR mutation looks
    equivalent.  Two layouts are needed because the witnesses pull in opposite
    directions:

    ``sliding``
        Each distinct ``column = literal`` pair is seeded into an overlapping
        window of two rows, so consecutive predicates share exactly one row and
        a conjunction differs from a disjunction.
    ``aligned``
        Every pair is seeded into the same two rows, so a multi-predicate query
        actually matches two rows and MIN/MAX/SUM/AVG mutations, boundary
        comparisons and DISTINCT all become observable.

    One extra row carries ``literal + 1`` so strict and non-strict comparisons
    are distinguished, and ``LIKE`` patterns contribute a satisfying value.
    """
    lookup: dict[str, tuple[str, str]] = {}
    protected: dict[str, set[str]] = {}
    for table in tables:
        protected[table.name] = _protected_columns(table)
        for column in table.columns:
            lookup.setdefault(_norm(column.name), (table.name, column.name))
    # ``seeds`` tuples are ``(table, column, value, needs_boundary)``.  The
    # boundary row (``value + 1``) only separates order-sensitive comparisons
    # (``<``/``<=``/``>``/``>=``); planting it for ``=`` or ``IN`` wastes a row
    # without sharpening any witness.
    seeds: list[tuple[str, str, Any, bool]] = []
    patterns: list[tuple[str, str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def remember(
        raw_column: str,
        value: Any,
        *,
        sink: list[tuple[str, str, Any]] | None = None,
        needs_boundary: bool = False,
    ) -> None:
        target = lookup.get(_norm(_unquote_identifier(raw_column)))
        if target is None:
            return
        table_name, column_name = target
        if column_name in protected.get(table_name, ()):
            return
        key = (table_name, column_name, repr(value))
        if key in seen:
            return
        seen.add(key)
        if sink is None:
            seeds.append((table_name, column_name, value, needs_boundary))
        else:
            sink.append((table_name, column_name, value))

    for sql in queries:
        for match in _LITERAL_PREDICATE.finditer(sql):
            try:
                value = _literal_value(match.group("value"))
            except ValueError:
                continue
            # ``needs_boundary`` only for order-sensitive comparisons; ``=`` and
            # ``<>``/``!=`` carry no strict-vs-non-strict distinction.
            needs_boundary = match.group("operator") in ("<", "<=", ">", ">=")
            remember(match.group("column"), value, needs_boundary=needs_boundary)
        for match in _LITERAL_MEMBERSHIP.finditer(sql):
            for token in _split_top_level(match.group("values")):
                try:
                    remember(match.group("column"), _literal_value(token))
                except ValueError:
                    continue
        for match in _LITERAL_PATTERN.finditer(sql):
            witness = _pattern_witness(match.group("value"))
            if witness is not None:
                remember(match.group("column"), witness, sink=patterns)

    for offset, (table_name, column_name, value, needs_boundary) in enumerate(seeds):
        table_rows = rows.get(table_name) or []
        count = len(table_rows)
        if not count:
            continue
        base = 0 if layout == "aligned" else offset
        table_rows[base % count][column_name] = value
        if count >= 2:
            table_rows[(base + 1) % count][column_name] = value
        if (
            needs_boundary
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and count >= 3
        ):
            table_rows[(base + 2) % count][column_name] = value + 1

    # LIKE witnesses land at the tail of the table.  ``_value_for`` reserves the
    # first rows for hand-built pattern sentinels such as ``DataX``/``XData``,
    # and overwriting those would remove the witness that separates ``'Data%'``
    # from ``'%Data'``.  Two tail rows share the value so DISTINCT stays
    # observable.
    for offset, (table_name, column_name, value) in enumerate(patterns):
        table_rows = rows.get(table_name) or []
        count = len(table_rows)
        if not count:
            continue
        table_rows[count - 1 - (offset % count)][column_name] = value
        if count >= 2:
            table_rows[count - 1 - ((offset + 1) % count)][column_name] = value


def _query_literal_specs(sql: str) -> list[tuple[str, str, Any]]:
    """Return simple ``column operator literal`` predicates from one query."""
    result: list[tuple[str, str, Any]] = []
    for match in _LITERAL_PREDICATE.finditer(sql):
        try:
            value = _literal_value(match.group("value"))
        except (TypeError, ValueError):
            continue
        result.append(
            (
                _unquote_identifier(match.group("column")),
                match.group("operator"),
                value,
            )
        )
    return result


def _comparison_boundary_pair(
    standard_sql: str,
    student_sql: str,
) -> tuple[str, str, Any] | None:
    """Find one strict/non-strict comparison shared by a query pair."""
    strict_pairs = {
        frozenset({">", ">="}),
        frozenset({"<", "<="}),
    }
    standard = _query_literal_specs(standard_sql)
    student = _query_literal_specs(student_sql)
    for standard_column, standard_operator, standard_value in standard:
        for student_column, student_operator, student_value in student:
            if (
                _norm(standard_column) == _norm(student_column)
                and standard_value == student_value
                and frozenset({standard_operator, student_operator}) in strict_pairs
                and standard_operator != student_operator
            ):
                return standard_column, standard_operator, standard_value
    return None


def _comparison_satisfying_value(operator: str, value: Any) -> Any:
    """Choose a small value satisfying one ordinary comparison."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if operator in {">", ">="}:
            return value + (1 if operator == ">" else 0)
        if operator in {"<", "<="}:
            return value - (1 if operator == "<" else 0)
        if operator in {"!=", "<>"}:
            return value + 1
        return value
    if operator in {"!=", "<>"}:
        return f"__gold_oracle_different__{value}"
    return value


def _comparison_failing_value(operator: str, value: Any) -> Any:
    """Choose a value that fails both sides of a strictness mutation."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if operator in {">", ">="}:
            return value - 1
        if operator in {"<", "<="}:
            return value + 1
        if operator in {"!=", "<>"}:
            return value
        return value + 1
    if operator in {"!=", "<>"}:
        return value
    return f"__gold_oracle_nonmatching__{value}"


def _simple_query_shape(sql: str) -> bool:
    """Limit compatibility probes to one-table, conjunction-friendly SQL."""
    upper = sql.upper()
    return not re.search(
        r"\b(?:JOIN|GROUP\s+BY|HAVING|UNION|INTERSECT|EXCEPT|WINDOW)\b|"
        r"\bSELECT\b[\s\S]*\bSELECT\b|\bOR\b",
        upper,
    )


def _aggregate_argument_specs(sql: str) -> list[tuple[str, str]]:
    """Return simple aggregate function/argument pairs in SELECT SQL."""
    pattern = re.compile(
        rf"(?is)\b(?P<function>SUM|AVG|MIN|MAX|COUNT)\s*\(\s*"
        rf"(?P<argument>\*|{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?)"
        rf"\s*\)"
    )
    result: list[tuple[str, str]] = []
    for match in pattern.finditer(sql):
        argument = match.group("argument").strip()
        if argument != "*":
            argument = _unquote_identifier(argument.split(".")[-1].strip())
        result.append((match.group("function").upper(), argument))
    return result


def _apply_comparison_boundary_witness(
    rows: dict[str, list[dict[str, Any]]],
    tables: Sequence[TableDef],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Align a strictness boundary with the rest of a simple WHERE clause.

    Literal seeding intentionally exercises each predicate independently.  A
    conjunction such as ``a > 0 AND b = 1`` needs one additional row where
    *both* paths are true, otherwise ``>`` versus ``>=`` can remain hidden
    behind an aggregate over an empty result.  This narrow probe owns only
    one-table, AND-shaped queries and leaves joins/subqueries to their other
    generators.
    """
    if not (_simple_query_shape(standard_sql) and _simple_query_shape(student_sql)):
        return
    boundary = _comparison_boundary_pair(standard_sql, student_sql)
    if boundary is None:
        return
    boundary_column, boundary_operator, boundary_value = boundary
    lookup = {
        _norm(column.name): (table.name, column.name)
        for table in tables
        for column in table.columns
    }
    target = lookup.get(_norm(boundary_column))
    if target is None or not rows.get(target[0]):
        return
    table_name, target_column = target
    table_rows = rows[table_name]
    predicates = _query_literal_specs(standard_sql) + _query_literal_specs(student_sql)
    # Deduplicate the two copies of unchanged predicates while retaining the
    # standard operator for the changed boundary.
    unique: list[tuple[str, str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for column, operator, value in predicates:
        key = (_norm(column), operator, repr(value))
        if key not in seen:
            seen.add(key)
            unique.append((column, operator, value))
    if not unique:
        return

    resolved: list[tuple[str, str, Any, str]] = []
    for column, operator, value in unique:
        physical = lookup.get(_norm(column))
        if physical is None or physical[0] != table_name:
            continue
        resolved.append((physical[1], operator, value, _norm(column)))
    if not resolved:
        return

    # Row zero is the distinguishing boundary row.  All other rows are made
    # non-qualifying through one ordinary predicate, which keeps MAX/SUM/AVG
    # output from being dominated by an unrelated generated row.
    for physical, operator, value, normalized in resolved:
        if normalized == _norm(boundary_column):
            table_rows[0][physical] = boundary_value
        else:
            table_rows[0][physical] = _comparison_satisfying_value(operator, value)
    guard = next(
        (
            (physical, operator, value)
            for physical, operator, value, normalized in resolved
            if normalized != _norm(boundary_column)
        ),
        (target_column, boundary_operator, boundary_value),
    )
    for row in table_rows[1:]:
        row[guard[0]] = _comparison_failing_value(guard[1], guard[2])

    aggregate_specs = _aggregate_argument_specs(standard_sql)
    student_specs = _aggregate_argument_specs(student_sql)
    if not aggregate_specs or len(aggregate_specs) != len(student_specs):
        return
    for (standard_function, standard_argument), (student_function, student_argument) in zip(
        aggregate_specs,
        student_specs,
    ):
        if standard_function != student_function:
            continue
        if standard_argument == "*" or _norm(standard_argument) in {
            _norm(item[0]) for item in resolved
        }:
            continue
        aggregate_target = lookup.get(_norm(standard_argument))
        if aggregate_target is None or aggregate_target[0] != table_name:
            continue
        # A single high boundary value makes the strictness difference visible
        # even when the aggregate argument was guessed as TEXT by the compact
        # schema parser. SQLite still applies MAX/MIN/SUM/AVG to the inserted
        # numeric value according to the aggregate's normal coercion rules.
        table_rows[0][aggregate_target[1]] = 900000


def _apply_aggregate_function_witness(
    rows: dict[str, list[dict[str, Any]]],
    tables: Sequence[TableDef],
    standard_sql: str,
    student_sql: str,
) -> None:
    """Give changed scalar aggregates a small numeric separation witness."""
    if not (_simple_query_shape(standard_sql) and _simple_query_shape(student_sql)):
        return
    standard = _aggregate_argument_specs(standard_sql)
    student = _aggregate_argument_specs(student_sql)
    if not standard or len(standard) != len(student):
        return
    lookup = {
        _norm(column.name): (table.name, column.name)
        for table in tables
        for column in table.columns
    }
    predicates = _query_literal_specs(standard_sql) + _query_literal_specs(student_sql)
    predicate_columns = {_norm(column) for column, _operator, _value in predicates}
    for (standard_function, standard_argument), (student_function, student_argument) in zip(
        standard,
        student,
    ):
        if standard_function == student_function:
            continue
        if standard_argument == "*" or _norm(standard_argument) != _norm(student_argument):
            continue
        target = lookup.get(_norm(standard_argument))
        if target is None or _norm(standard_argument) in predicate_columns:
            continue
        table_rows = rows.get(target[0]) or []
        if len(table_rows) < 2:
            continue

        # Re-align simple filters on the first two rows.  This is needed for
        # filtered aggregates whose equality literal was planted in a sliding
        # position by the generic literal seeder.
        for column, operator, value in predicates:
            physical = lookup.get(_norm(column))
            if physical is None or physical[0] != target[0]:
                continue
            planted = (
                value
                if operator in {"=", "!=", "<>"}
                else _comparison_satisfying_value(operator, value)
            )
            if operator in {"!=", "<>"}:
                planted = _comparison_satisfying_value(operator, value)
            for row in table_rows[:2]:
                row[physical[1]] = planted

        aggregate_column = target[1]
        table_rows[0][aggregate_column] = 1
        table_rows[1][aggregate_column] = 9
        for row in table_rows[2:]:
            row[aggregate_column] = 0


def _apply_subquery_membership_paths(
    rows: dict[str, list[dict[str, Any]]],
    tables: Sequence[TableDef],
    queries: Sequence[str],
) -> None:
    """Plant a partial-key witness for simple ``IN (SELECT ...)`` queries.

    Independently generated primary keys often use the same ``1..n`` sequence
    in both tables.  That is valid fixture data, but it makes an uncorrelated
    ``IN``/``EXISTS`` mutation accidentally look equivalent.  For a bounded,
    two-table membership shape, one outer key is assigned a value that is not
    present in the lookup table while the lookup remains non-empty.  The
    resulting row is admitted by ``EXISTS`` and rejected by ``IN``.

    This helper intentionally handles only the uncorrelated, single-column
    shape. Correlated subqueries, expressions, composite keys, and nested
    SELECTs are left to the ordinary generated worlds rather than guessed.
    """
    table_by_name = {_norm(table.name): table for table in tables}
    if len(queries) != 2 or not table_by_name:
        return

    def identifier(value: str | None) -> str:
        return _unquote_identifier(str(value or "").strip().rstrip("."))

    def first_outer_table(sql: str, end: int) -> str | None:
        prefix = sql[:end]
        match = re.search(rf"(?is)\bFROM\s+(?P<table>{_IDENTIFIER})", prefix)
        if match is None:
            return None
        return identifier(match.group("table"))

    def column(table_name: str, column_name: str) -> ColumnDef | None:
        table = table_by_name.get(_norm(table_name))
        if table is None:
            return None
        wanted = _norm(column_name)
        return next((item for item in table.columns if _norm(item.name) == wanted), None)

    for query in queries:
        match = _SUBQUERY_MEMBERSHIP.search(query)
        if match is None:
            continue
        outer_qualifier = identifier(match.group("outer_qual"))
        inner_qualifier = identifier(match.group("inner_qual"))
        outer_table_name = (
            outer_qualifier
            if outer_qualifier and _norm(outer_qualifier) in table_by_name
            else first_outer_table(query, match.start())
        )
        inner_table_name = identifier(match.group("inner_table"))
        if not outer_table_name or not inner_table_name:
            continue
        if _norm(outer_table_name) == _norm(inner_table_name):
            continue
        outer_table = table_by_name.get(_norm(outer_table_name))
        inner_table = table_by_name.get(_norm(inner_table_name))
        if outer_table is None or inner_table is None:
            continue
        outer_column = column(outer_table.name, identifier(match.group("outer_column")))
        inner_column = column(inner_table.name, identifier(match.group("inner_column")))
        if outer_column is None or inner_column is None:
            continue
        outer_rows = rows.get(outer_table.name) or []
        inner_rows = rows.get(inner_table.name) or []
        if not outer_rows or not inner_rows:
            continue

        def marker(definition: ColumnDef, prefix: str) -> Any:
            kind = _sqlite_type(definition.declared_type)
            if kind == "INTEGER":
                return -100000000 if prefix == "outer" else -200000000
            if kind == "REAL":
                return -100000000.5 if prefix == "outer" else -200000000.5
            return f"__gold_membership_{prefix}_nonmatching__"

        # Keep the lookup non-empty but make the first outer row a guaranteed
        # non-member.  Only one row is changed to preserve unrelated predicate
        # boundaries and declared uniqueness constraints.
        outer_rows[0][outer_column.name] = marker(outer_column, "outer")
        inner_rows[0][inner_column.name] = marker(inner_column, "inner")
        return


def _apply_count_null_paths(
    rows: dict[str, list[dict[str, Any]]],
    tables: Sequence[TableDef],
    queries: Sequence[str],
) -> None:
    """Add a NULL-sensitive path for simple filtered COUNT mutations.

    Ordinary seed values keep the first two rows non-NULL, which makes
    ``COUNT(column)`` and ``COUNT(*)`` indistinguishable even when the source
    column is nullable. This pass is conservative: it only handles a
    single-table query with one literal equality, leaves predicate columns and
    constrained columns alone, and requires two reachable rows.
    """
    if len(queries) != 2:
        return

    specs: list[tuple[str, str, str, Any]] = []
    for query in queries:
        if len(re.findall(r"(?is)\bselect\b", query)) != 1:
            continue
        match = re.match(
            rf"(?is)^\s*SELECT\s+COUNT\s*\(\s*(?P<count>\*|{_IDENTIFIER})\s*\)\s+"
            rf"FROM\s+(?P<table>{_IDENTIFIER})\s+WHERE\s+(?P<predicate>.+?)\s*$",
            query,
        )
        if not match or re.search(
            r"(?is)\b(?:JOIN|GROUP\s+BY|HAVING|UNION|INTERSECT|EXCEPT)\b", query
        ):
            continue
        predicate = _LITERAL_PREDICATE.fullmatch(match.group("predicate").strip())
        if predicate is None or predicate.group("operator") != "=":
            continue
        try:
            value = _literal_value(predicate.group("value"))
        except ValueError:
            continue
        specs.append(
            (
                _unquote_identifier(match.group("table")),
                _unquote_identifier(match.group("count"))
                if match.group("count") != "*"
                else "*",
                _unquote_identifier(predicate.group("column")),
                value,
            )
        )

    if len(specs) != 2:
        return
    star_specs = [item for item in specs if item[1] == "*"]
    column_specs = [item for item in specs if item[1] != "*"]
    if len(star_specs) != 1 or len(column_specs) != 1:
        return
    star_table, _star_arg, star_predicate, star_value = star_specs[0]
    column_table, count_column, column_predicate, column_value = column_specs[0]
    if (
        _norm(star_table) != _norm(column_table)
        or _norm(star_predicate) != _norm(column_predicate)
        or star_value != column_value
        or _norm(count_column) == _norm(star_predicate)
    ):
        return

    table = next((item for item in tables if _norm(item.name) == _norm(star_table)), None)
    table_rows = rows.get(table.name if table else "") if table else None
    if table is None or not table_rows or len(table_rows) < 2:
        return
    predicate_def = next(
        (item for item in table.columns if _norm(item.name) == _norm(star_predicate)),
        None,
    )
    count_def = next(
        (item for item in table.columns if _norm(item.name) == _norm(count_column)),
        None,
    )
    if (
        predicate_def is None
        or count_def is None
        or not count_def.nullable
        or count_def.primary_key
        or count_def.unique
        or predicate_def.primary_key
    ):
        return

    # Keep both rows reachable through the literal predicate. Do not write a
    # NULL into the predicate column or into a constrained count column.
    table_rows[0][predicate_def.name] = star_value
    table_rows[1][predicate_def.name] = star_value
    kind = _sqlite_type(count_def.declared_type)
    non_null: Any = (
        1 if kind == "INTEGER" else 1.5 if kind == "REAL" else "__count_non_null__"
    )
    table_rows[0][count_def.name] = non_null
    table_rows[1][count_def.name] = None


def _apply_join_gaps(
    rows: dict[str, list[dict[str, Any]]],
    tables: Sequence[TableDef],
    queries: Sequence[str],
) -> None:
    """Give the driving table one row whose join key matches nothing.

    Without an unmatched row an INNER JOIN and a LEFT JOIN return the same
    result, so the ``INNER`` versus ``LEFT`` error family can never be
    distinguished.  Only the last row is touched, and never a key column.
    """
    lookup: dict[str, tuple[str, str]] = {}
    protected: dict[str, set[str]] = {}
    for table in tables:
        protected[table.name] = _protected_columns(table)
        for column in table.columns:
            lookup.setdefault(_norm(column.name), (table.name, column.name))
    for sql in queries:
        for match in _JOIN_EQUALITY.finditer(sql):
            target = lookup.get(_norm(_unquote_identifier(match.group("left"))))
            if target is None:
                continue
            table_name, column_name = target
            if column_name in protected.get(table_name, ()):
                continue
            table_rows = rows.get(table_name) or []
            if len(table_rows) < 2:
                continue
            current = table_rows[-1].get(column_name)
            table_rows[-1][column_name] = (
                _UNMATCHED_TEXT if isinstance(current, str) else _UNMATCHED_NUMBER
            )
            return


def _promote_numeric_columns(
    tables: Sequence[TableDef], queries: Sequence[str]
) -> list[TableDef]:
    """Refine guessed column types from the literals the queries compare against.

    A compact schema such as ``scores(average, interview)`` states no types, so
    every column defaults to TEXT.  SQLite then stores the seeded value ``8.823``
    as the string ``'8.823'``, and because TEXT always sorts above numbers in
    SQLite, ``average < 8.823`` and ``average <= 8.823`` are both false and the
    boundary mutation can never be distinguished.  Columns the queries compare
    against a number are therefore promoted to a numeric affinity, but only when
    the source did not state a type of its own.
    """
    numeric: dict[str, str] = {}
    for sql in queries:
        for match in _LITERAL_PREDICATE.finditer(sql):
            token = match.group("value").strip()
            if token.startswith(("'", '"')):
                continue
            key = _norm(_unquote_identifier(match.group("column")))
            if "." in token:
                numeric[key] = "REAL"
            else:
                numeric.setdefault(key, "INTEGER")
    if not numeric:
        return list(tables)
    promoted: list[TableDef] = []
    for table in tables:
        columns = []
        for column in table.columns:
            wanted = numeric.get(_norm(column.name))
            if (
                wanted is not None
                and not column.type_declared
                and _sqlite_type(column.declared_type) == "TEXT"
            ):
                columns.append(replace(column, declared_type=wanted))
            else:
                columns.append(column)
        promoted.append(
            TableDef(
                name=table.name,
                columns=columns,
                primary_key=table.primary_key,
                unique_constraints=list(table.unique_constraints),
                foreign_keys=list(table.foreign_keys),
            )
        )
    return promoted


def _quote_numeric_schema_identifiers(sql: str, tables: Sequence[TableDef]) -> str:
    """Quote numeric-leading schema names for the bounded SQLite runner.

    WikiSQL headers such as ``2007`` are identifiers in the source dialect but
    are parsed as numeric literals by SQLite when left bare.  The production
    parser already applies a schema-aware repair; the independent Gold Oracle
    must execute the same source query semantics.  Only names declared by the
    supplied schema are rewritten, and strings/comments/quoted identifiers are
    copied byte-for-byte.
    """
    names = {
        column.name.casefold()
        for table in tables
        for column in table.columns
        if column.name and column.name[0].isdigit()
    }
    names.update(
        table.name.casefold()
        for table in tables
        if table.name and table.name[0].isdigit()
    )
    if not names:
        return sql
    output: list[str] = []
    index = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if line_comment:
            output.append(char)
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            output.append(char)
            if char == "*" and next_char == "/":
                output.append(next_char)
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "-" and next_char == "-":
            output.extend((char, next_char))
            index += 2
            line_comment = True
            continue
        if char == "/" and next_char == "*":
            output.extend((char, next_char))
            index += 2
            block_comment = True
            continue
        if char in "'\"`[":
            quote = "]" if char == "[" else char
            output.append(char)
            index += 1
            continue
        if char.isdigit() and (
            index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] in "_$")
        ):
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] in "_$"):
                end += 1
            token = sql[index:end]
            if token.casefold() in names:
                output.append('"' + token.replace('"', '""') + '"')
            else:
                output.append(token)
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _load_world(
    connection: sqlite3.Connection,
    tables: Sequence[TableDef],
    rows: dict[str, list[dict[str, Any]]],
) -> None:
    connection.execute("PRAGMA foreign_keys = OFF")
    for table in tables:
        connection.execute(_create_table_sql(table))
    for table in tables:
        columns = [column.name for column in table.columns]
        placeholders = ",".join("?" for _ in columns)
        statement = (
            f"INSERT INTO {_quote_identifier(table.name)} ("
            + ",".join(_quote_identifier(column) for column in columns)
            + f") VALUES ({placeholders})"
        )
        for row in rows[table.name]:
            connection.execute(statement, [row[column] for column in columns])
    connection.commit()


def _query_result(
    connection: sqlite3.Connection,
    sql: str,
    *,
    max_result_rows: int,
    max_vm_steps: int,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    steps = 0

    def progress() -> int:
        nonlocal steps
        steps += 1
        return 1 if steps * 1000 > max_vm_steps else 0

    connection.set_progress_handler(progress, 1000)
    connection.set_authorizer(_read_only_authorizer)
    try:
        cursor = connection.execute(sql)
        columns = [str(item[0]) for item in (cursor.description or ())]
        rows = cursor.fetchmany(max_result_rows + 1)
        if len(rows) > max_result_rows:
            raise OracleEngineGap("result row limit exceeded")
        return columns, [tuple(row) for row in rows]
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "interrupted" in message or "too many" in message:
            raise OracleEngineGap("SQLite VM or result limit exceeded") from exc
        raise
    finally:
        connection.set_authorizer(None)
        connection.set_progress_handler(None, 0)


# ---------------------------------------------------------------------------
# Native runners
#
# Each runner owns one short-lived database: it builds the schema, loads the
# generated rows, runs one read-only query, and tears everything down.  The
# SQLite runner is the in-process default; MySQL/PostgreSQL runners connect to
# the engines wired through ``PARSEVAL_*_URL`` and reset their state per query
# pair so no world leaks into the next.
# ---------------------------------------------------------------------------


class _NativeRunner:
    """Shared interface for the SQLite and native DB runners."""

    def execute(
        self,
        sql: str,
        *,
        max_result_rows: int,
        max_vm_steps: int,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def metadata(self) -> dict[str, Any]:
        return {"adapter": type(self).__name__}


class _SQLiteRunner(_NativeRunner):
    def __init__(self, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> None:
        self.connection = sqlite3.connect(":memory:")
        try:
            _load_world(self.connection, tables, rows)
        except Exception:
            self.connection.close()
            raise

    def execute(
        self,
        sql: str,
        *,
        max_result_rows: int,
        max_vm_steps: int,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        return _query_result(
            self.connection,
            sql,
            max_result_rows=max_result_rows,
            max_vm_steps=max_vm_steps,
        )

    def close(self) -> None:
        self.connection.close()


@contextmanager
def _native_runner(
    dialect: str,
    tables: Sequence["TableDef"],
    rows: dict[str, list[dict[str, Any]]],
) -> Iterator[_NativeRunner]:
    """Yield a runner for one query pair, then tear it down."""
    normalized = _norm(dialect)
    url = _native_dialect_url(normalized)
    runner: _NativeRunner
    engine = NATIVE_DIALECTS.get(normalized, (normalized, ""))[0]
    if engine == "mysql" and url:
        runner = _MySQLRunner.from_url(url, tables, rows)
    elif engine == "postgres" and url:
        runner = _PostgresRunner.from_url(url, tables, rows)
    elif engine == "tsql" and url:
        runner = _TSQLRunner.from_url(url, tables, rows)
    elif engine == "oracle" and url:
        runner = _OracleRunner.from_url(url, tables, rows)
    else:
        runner = _SQLiteRunner(tables, rows)
    try:
        yield runner
    finally:
        runner.close()


def _ddl_statements(table: "TableDef") -> list[str]:
    """Return a CREATE TABLE for the runner dialect, plus any FK constraints.

    The generated DDL keeps declared types/constraints so the native engine's
    own typing and three-valued logic participate in the oracle verdict.
    """
    definitions: list[str] = []
    primary = set(table.primary_key)
    for column in table.columns:
        type_name = column.declared_type or "TEXT"
        definition = f"{_quote_identifier(column.name)} {type_name}"
        if column.primary_key and len(table.primary_key) <= 1:
            definition += " PRIMARY KEY"
        if column.unique:
            definition += " UNIQUE"
        if not column.nullable:
            definition += " NOT NULL"
        definitions.append(definition)
    if len(table.primary_key) > 1:
        definitions.append(
            "PRIMARY KEY (" + ", ".join(_quote_identifier(item) for item in table.primary_key) + ")"
        )
    for unique in table.unique_constraints:
        definitions.append(
            "UNIQUE (" + ", ".join(_quote_identifier(item) for item in unique) + ")"
        )
    for foreign_key in table.foreign_keys:
        definitions.append(
            "FOREIGN KEY (" + ", ".join(_quote_identifier(item) for item in foreign_key.columns) + ") "
            "REFERENCES " + _quote_identifier(foreign_key.references_table) + " ("
            + ", ".join(_quote_identifier(item) for item in foreign_key.references_columns) + ")"
        )
    return [f"CREATE TABLE {_quote_identifier(table.name)} (" + ", ".join(definitions) + ")"]


def _backtick_identifier(value: str) -> str:
    """Quote an identifier with MySQL backticks (MySQL lacks ANSI double quotes)."""
    text = str(value or "").strip()
    if not text or "\x00" in text or "." in text:
        raise OracleInputError(f"unsafe table or column identifier: {value!r}")
    return "`" + text.replace("`", "``") + "`"


def _ddl_statements_mysql(table: "TableDef") -> list[str]:
    """MySQL CREATE TABLE: backtick identifiers and type names MySQL accepts."""
    definitions: list[str] = []
    for column in table.columns:
        declared = (column.declared_type or "TEXT").upper()
        kind = _sqlite_type(declared)
        mysql_type = {"INTEGER": "INT", "REAL": "DOUBLE", "BLOB": "BLOB"}.get(kind, "VARCHAR(255)")
        # A declared length/type such as VARCHAR(40) is preserved verbatim when it already looks native.
        if re.match(r"^[A-Z]+\(.*\)$", declared) or declared not in {"", "TEXT", "INTEGER", "REAL", "BLOB"}:
            mysql_type = declared
        definition = f"{_backtick_identifier(column.name)} {mysql_type}"
        if column.primary_key and len(table.primary_key) <= 1:
            definition += " PRIMARY KEY"
        if column.unique:
            definition += " UNIQUE"
        if not column.nullable:
            definition += " NOT NULL"
        definitions.append(definition)
    if len(table.primary_key) > 1:
        definitions.append(
            "PRIMARY KEY (" + ", ".join(_backtick_identifier(item) for item in table.primary_key) + ")"
        )
    for unique in table.unique_constraints:
        definitions.append(
            "UNIQUE (" + ", ".join(_backtick_identifier(item) for item in unique) + ")"
        )
    return [f"CREATE TABLE {_backtick_identifier(table.name)} (" + ", ".join(definitions) + ")"]


def _vendor_type(declared: str, backend: str) -> str:
    """Map compact-schema types to conservative native DDL types."""

    text = (declared or "TEXT").strip().upper()
    affinity = _sqlite_type(text)
    if backend == "tsql":
        if affinity == "INTEGER":
            return "BIGINT"
        if affinity == "REAL":
            return "FLOAT"
        if affinity == "BLOB":
            return "VARBINARY(MAX)"
        if re.fullmatch(r"(?:N?VARCHAR|N?CHAR)\s*\(\s*\d+\s*\)", text):
            return text
        return "NVARCHAR(4000)"
    if affinity == "INTEGER":
        return "NUMBER(19)"
    if affinity == "REAL":
        return "BINARY_DOUBLE"
    if affinity == "BLOB":
        return "BLOB"
    if re.fullmatch(r"(?:VARCHAR2|VARCHAR|CHAR)\s*\(\s*\d+(?:\s+CHAR)?\s*\)", text):
        return text
    return "VARCHAR2(4000 CHAR)"


def _ddl_statements_vendor(table: "TableDef", backend: str) -> list[str]:
    """Render only fixture DDL; submitted SQL remains untouched."""

    definitions: list[str] = []
    quote = _bracket_identifier if backend == "tsql" else _oracle_identifier
    for column in table.columns:
        definition = f"{quote(column.name)} {_vendor_type(column.declared_type, backend)}"
        if column.primary_key and len(table.primary_key) <= 1:
            definition += " PRIMARY KEY"
        if column.unique:
            definition += " UNIQUE"
        if not column.nullable:
            definition += " NOT NULL"
        definitions.append(definition)
    if len(table.primary_key) > 1:
        definitions.append(
            "PRIMARY KEY (" + ", ".join(quote(item) for item in table.primary_key) + ")"
        )
    for unique in table.unique_constraints:
        definitions.append(
            "UNIQUE (" + ", ".join(quote(item) for item in unique) + ")"
        )
    return [f"CREATE TABLE {quote(table.name)} (" + ", ".join(definitions) + ")"]


def _bracket_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text or "." in text:
        raise OracleInputError(f"unsafe table or column identifier: {value!r}")
    return "[" + text.replace("]", "]]") + "]"


def _oracle_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text or "." in text:
        raise OracleInputError(f"unsafe table or column identifier: {value!r}")
    # Unquoted Oracle identifiers are upper-cased by the engine. Quoting the
    # upper-cased spelling lets the same submitted lower-case teaching SQL
    # resolve naturally while avoiding dependence on session NLS settings.
    return '"' + text.upper().replace('\"', '\"\"') + '"'


def _ddl_statements_for(dialect: str, table: "TableDef") -> list[str]:
    """Dispatch DDL generation by runner dialect."""
    if dialect == "mysql":
        return _ddl_statements_mysql(table)
    if dialect in {"tsql", "oracle"}:
        return _ddl_statements_vendor(table, dialect)
    return _ddl_statements(table)


def _coerce_python_value(value: Any) -> Any:
    """Normalize a generated oracle value for a parameterized native insert."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fit_native_value(column: "ColumnDef", value: Any) -> Any:
    """Keep generated fixture rows valid under declared native text widths."""
    limit = _declared_text_limit(column.declared_type)
    if limit is not None and isinstance(value, str):
        if len(value) <= limit:
            return value
        if column.primary_key or column.unique:
            # Blind prefix truncation can collapse values such as
            # ``corporate_number_0``/``corporate_number_1`` onto the same
            # CHAR/VARCHAR primary key and turn a valid public world into an
            # ENGINE_GAP.  Preserve a deterministic suffix derived from the
            # full generated value.  This is fixture fitting only; submitted
            # SQL and source literals are never rewritten.
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            if limit <= 8:
                return digest[:limit]
            suffix = "_" + digest[:8]
            return value[: limit - len(suffix)] + suffix
        return value[:limit]
    return value


class _MySQLRunner(_NativeRunner):
    """A short-lived MySQL runner reached through ``PARSEVAL_MYSQL_URL``."""

    def __init__(self, connection: Any, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> None:
        self.connection = connection
        try:
            self._materialize(tables, rows)
        except Exception:
            self.close()
            raise

    @classmethod
    def from_url(cls, url: str, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> "_MySQLRunner":
        import pymysql  # local import keeps the module importable without the driver

        params = _parse_db_url(url, default_port=3306)
        connection = pymysql.connect(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            # The URL database is often a deployment-specific default that is
            # not present in a clean test service. Create and select an
            # isolated schema below instead of requiring that default first.
            database=None,
            autocommit=True,
            charset="utf8mb4",
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
        )
        return cls(connection, tables, rows)

    def _materialize(self, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> None:
        cursor = self.connection.cursor()
        try:
            try:
                cursor.execute("SELECT VERSION(), @@lower_case_table_names")
                profile = cursor.fetchone()
            except Exception as exc:
                raise OracleEngineGap("mysql target profile probe failed") from exc
            if not profile or len(profile) < 2:
                raise OracleEngineGap("mysql target profile probe returned no row")
            version = str(profile[0] or "").strip()
            try:
                lower_case_table_names = int(profile[1])
            except (TypeError, ValueError) as exc:
                raise OracleEngineGap("mysql identifier mode probe was not numeric") from exc
            if not re.fullmatch(r"8\.0\.46(?:[- ].*)?", version):
                raise OracleEngineGap(f"mysql target requires version {MYSQL_TARGET_VERSION}")
            if lower_case_table_names != MYSQL_REQUIRED_LOWER_CASE_TABLE_NAMES:
                raise OracleEngineGap(
                    "mysql target requires lower_case_table_names=0 with source-spelled fixtures"
                )
            self._mysql_profile = {
                "version": MYSQL_TARGET_VERSION,
                "lower_case_table_names": MYSQL_REQUIRED_LOWER_CASE_TABLE_NAMES,
                "fixture_identifier_policy": MYSQL_FIXTURE_IDENTIFIER_POLICY,
            }
            cursor.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'")
            cursor.execute("SET SESSION FOREIGN_KEY_CHECKS = 0")
            # A dedicated schema per world keeps concurrent oracle runs isolated
            # without spinning up a new database server per query pair.
            self._schema = f"oracle_{_world_token()}"
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS `{self._schema}`")
            cursor.execute(f"USE `{self._schema}`")
            for table in tables:
                cursor.execute(f"DROP TABLE IF EXISTS `{table.name}`")
                for statement in _ddl_statements_for("mysql", table):
                    cursor.execute(statement)
            for table in tables:
                self._insert_rows(cursor, table, rows.get(table.name, []))
        finally:
            cursor.close()

    def _insert_rows(self, cursor: Any, table: "TableDef", rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        columns = [column.name for column in table.columns]
        placeholders = ",".join(["%s"] * len(columns))
        statement = (
            f"INSERT INTO `{table.name}` ("
            + ",".join(f"`{column}`" for column in columns)
            + f") VALUES ({placeholders})"
        )
        payload = [[_coerce_python_value(_fit_native_value(table.columns[index], row[column])) for index, column in enumerate(columns)] for row in rows]
        cursor.executemany(statement, payload)

    def execute(
        self,
        sql: str,
        *,
        max_result_rows: int,
        max_vm_steps: int,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
            columns = [str(item[0]) for item in (cursor.description or ())]
            rows = cursor.fetchmany(max_result_rows + 1)
            if len(rows) > max_result_rows:
                raise OracleEngineGap("result row limit exceeded")
            return columns, [tuple(row) for row in rows]
        finally:
            cursor.close()

    def close(self) -> None:
        schema = getattr(self, "_schema", None)
        connection = getattr(self, "connection", None)
        try:
            if connection is not None and schema is not None:
                cursor = connection.cursor()
                try:
                    cursor.execute(f"DROP SCHEMA IF EXISTS `{schema}`")
                finally:
                    cursor.close()
        except Exception:
            pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def metadata(self) -> dict[str, Any]:
        return {
            "adapter": type(self).__name__,
            "engine_profile": dict(getattr(self, "_mysql_profile", {})),
        }


_POSTGRES_IDENTIFIER_RE = r'(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)'


def _postgres_identifier_value(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace('""', '"')
    return text.lower()


def _postgres_sql_segments(sql: str) -> list[tuple[bool, str]]:
    """Split PostgreSQL SQL into code and protected literal/comment spans.

    The independent oracle intentionally does not depend on the production
    SQL parser.  This small lexer is only used to keep fixture-schema rewrites
    out of string literals, dollar-quoted bodies and comments.
    """

    text = str(sql or "")
    segments: list[tuple[bool, str]] = []
    start = 0
    index = 0
    while index < len(text):
        protected_end: int | None = None
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            protected_end = len(text) if newline < 0 else newline
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            protected_end = len(text) if close < 0 else close + 2
        elif text[index] == "'":
            cursor = index + 1
            while cursor < len(text):
                if text[cursor] == "'":
                    if cursor + 1 < len(text) and text[cursor + 1] == "'":
                        cursor += 2
                        continue
                    cursor += 1
                    break
                cursor += 1
            protected_end = cursor
        elif text[index] == "$":
            marker = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", text[index:])
            if marker:
                token = marker.group(0)
                close = text.find(token, index + len(token))
                protected_end = len(text) if close < 0 else close + len(token)
        if protected_end is None:
            index += 1
            continue
        if start < index:
            segments.append((True, text[start:index]))
        segments.append((False, text[index:protected_end]))
        index = protected_end
        start = index
    if start < len(text):
        segments.append((True, text[start:]))
    return segments or [(True, text)]


def _rewrite_postgres_fixture_schemas(
    sql: str,
    table_names: Iterable[str],
    fixture_schema: str,
) -> str:
    """Map explicit source schemas onto the per-world fixture schema.

    Public PostgreSQL teaching material commonly names logical tables as
    ``cd.bookings`` while the reproducible schema catalog stores the table as
    ``bookings``.  The runner materializes every world in a unique schema.
    Only qualifiers proven by a FROM/JOIN reference to a declared fixture
    table are rewritten; aliases, columns, literals and comments are left
    untouched.
    """

    known_tables = {str(name).lower() for name in table_names if str(name).strip()}
    if not known_tables:
        return sql
    segments = _postgres_sql_segments(sql)
    reference = re.compile(
        rf"(?is)\b(?:FROM|JOIN)\s+"
        rf"(?P<schema>{_POSTGRES_IDENTIFIER_RE})\s*\.\s*"
        rf"(?P<table>{_POSTGRES_IDENTIFIER_RE})"
    )
    source_schemas: set[str] = set()
    for is_code, chunk in segments:
        if not is_code:
            continue
        for match in reference.finditer(chunk):
            table = _postgres_identifier_value(match.group("table")).lower()
            if table in known_tables:
                source_schemas.add(
                    _postgres_identifier_value(match.group("schema")).lower()
                )
    if not source_schemas:
        return sql
    qualified = re.compile(
        rf"(?is)(?<![A-Za-z0-9_$])"
        rf"(?P<schema>{_POSTGRES_IDENTIFIER_RE})\s*\.\s*"
        rf"(?P<table>{_POSTGRES_IDENTIFIER_RE})"
        rf"(?![A-Za-z0-9_$])"
    )
    target = '"' + str(fixture_schema).replace('"', '""') + '"'

    def replace_schema(match: re.Match[str]) -> str:
        schema = _postgres_identifier_value(match.group("schema")).lower()
        table = _postgres_identifier_value(match.group("table")).lower()
        if schema not in source_schemas or table not in known_tables:
            return match.group(0)
        return f"{target}.{match.group('table')}"

    return "".join(
        qualified.sub(replace_schema, chunk) if is_code else chunk
        for is_code, chunk in segments
    )


class _PostgresRunner(_NativeRunner):
    """A short-lived PostgreSQL runner reached through ``PARSEVAL_POSTGRES_URL``."""

    def __init__(self, connection: Any, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> None:
        self.connection = connection
        self._table_names = frozenset(table.name.lower() for table in tables)
        try:
            self._materialize(tables, rows)
        except Exception:
            self.close()
            raise

    @classmethod
    def from_url(cls, url: str, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> "_PostgresRunner":
        import psycopg  # local import keeps the module importable without the driver

        params = _parse_db_url(url, default_port=5432)
        connection = psycopg.connect(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            dbname=params["database"],
            autocommit=True,
            connect_timeout=10,
        )
        return cls(connection, tables, rows)

    def _materialize(self, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> None:
        cursor = self.connection.cursor()
        try:
            cursor.execute("SET SESSION constraint_exclusion = off")
            # Recursive teaching queries must not be allowed to consume the
            # whole database container before the outer Python timeout fires.
            cursor.execute("SET SESSION statement_timeout = 5000")
            cursor.execute("SET SESSION lock_timeout = 2000")
            cursor.execute("SET SESSION work_mem = '2MB'")
            cursor.execute("SET SESSION temp_file_limit = '64MB'")
            # Connection-local TEMP tables isolate worlds without generating
            # hundreds of megabytes of WAL from CREATE/DROP SCHEMA cycles.
            # Closing the connection removes the complete fixture atomically.
            self._schema = "pg_temp"
            cursor.execute("SET search_path TO pg_temp")
            for table in tables:
                for statement in _ddl_statements(table):
                    cursor.execute(_postgres_temp_table_ddl(statement))
            for table in tables:
                self._insert_rows(cursor, table, rows.get(table.name, []))
        finally:
            cursor.close()

    def _insert_rows(self, cursor: Any, table: "TableDef", rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        columns = [column.name for column in table.columns]
        placeholders = ",".join(["%s"] * len(columns))
        statement = (
            f'INSERT INTO "{table.name}" ('
            + ",".join(f'"{column}"' for column in columns)
            + f") VALUES ({placeholders})"
        )
        payload = [[_coerce_python_value(_fit_native_value(table.columns[index], row[column])) for index, column in enumerate(columns)] for row in rows]
        cursor.executemany(statement, payload)

    def execute(
        self,
        sql: str,
        *,
        max_result_rows: int,
        max_vm_steps: int,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        cursor = self.connection.cursor()
        try:
            executable_sql = _rewrite_postgres_fixture_schemas(
                sql,
                self._table_names,
                self._schema,
            )
            cursor.execute(executable_sql)
            columns = [str(item[0]) for item in (cursor.description or ())]
            rows = cursor.fetchmany(max_result_rows + 1)
            if len(rows) > max_result_rows:
                raise OracleEngineGap("result row limit exceeded")
            return columns, [tuple(row) for row in rows]
        finally:
            cursor.close()

    def metadata(self) -> dict[str, Any]:
        return {
            "adapter": type(self).__name__,
            "isolated_schema": True,
            "qualified_fixture_table_rewrite": True,
            "temporary_tables": True,
            "statement_timeout_ms": 5000,
        }

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _postgres_temp_table_ddl(statement: str) -> str:
    """Convert fixture CREATE TABLE DDL to connection-local TEMP DDL."""

    text = str(statement or "").strip()
    if not re.match(r"(?is)^CREATE\s+TABLE\b", text):
        raise OracleInputError("PostgreSQL fixture DDL must start with CREATE TABLE")
    return re.sub(r"(?is)^CREATE\s+TABLE\b", "CREATE TEMP TABLE", text, count=1)


def _odbc_value(value: str) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def _tsql_connection_string(url: str) -> str:
    parsed = urlsplit(url.strip())
    query = {key.lower(): values[-1] for key, values in parse_qs(parsed.query).items() if values}
    driver = query.get("driver", "ODBC Driver 18 for SQL Server")
    server = parsed.hostname or "127.0.0.1"
    if parsed.port:
        server = f"{server},{parsed.port}"
    database = (parsed.path or "").strip("/") or "master"
    parts = [
        f"DRIVER={_odbc_value(driver)}",
        f"SERVER={_odbc_value(server)}",
        f"DATABASE={_odbc_value(database)}",
    ]
    if parsed.username:
        parts.extend((
            f"UID={_odbc_value(unquote(parsed.username))}",
            f"PWD={_odbc_value(unquote(parsed.password or ''))}",
        ))
    else:
        parts.append("Trusted_Connection={yes}")
    parts.extend(("Encrypt={yes}", "TrustServerCertificate={yes}"))
    return ";".join(parts)


class _TSQLRunner(_NativeRunner):
    """Independent SQL Server world runner for Gold Oracle audits."""

    def __init__(self, connection: Any, database: str, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> None:
        self.connection = connection
        self._database = database
        try:
            self._materialize(tables, rows)
        except Exception:
            self.close()
            raise

    @classmethod
    def from_url(cls, url: str, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> "_TSQLRunner":
        import pyodbc

        database = f"gold_{_world_token()}"
        connection = pyodbc.connect(_tsql_connection_string(url), autocommit=True, timeout=10)
        cursor = connection.cursor()
        try:
            cursor.execute("USE [master]")
            cursor.execute(f"CREATE DATABASE {_bracket_identifier(database)}")
            cursor.execute(f"USE {_bracket_identifier(database)}")
        except Exception:
            try:
                cursor.close()
            finally:
                connection.close()
            raise
        finally:
            try:
                cursor.close()
            except Exception:
                pass
        return cls(connection, database, tables, rows)

    def _materialize(self, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> None:
        cursor = self.connection.cursor()
        try:
            try:
                self.connection.timeout = 30
            except (AttributeError, TypeError):
                pass
            cursor.execute("SET NOCOUNT ON")
            cursor.execute("SET ANSI_NULLS ON")
            cursor.execute("SET QUOTED_IDENTIFIER ON")
            for table in tables:
                for statement in _ddl_statements_for("tsql", table):
                    cursor.execute(statement)
                _insert_native_rows(cursor, "tsql", table, rows.get(table.name, []))
        finally:
            cursor.close()

    def execute(self, sql: str, *, max_result_rows: int, max_vm_steps: int) -> tuple[list[str], list[tuple[Any, ...]]]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql)
            columns = [str(item[0]) for item in (cursor.description or ())]
            rows = cursor.fetchmany(max_result_rows + 1)
            if len(rows) > max_result_rows:
                raise OracleEngineGap("result row limit exceeded")
            return columns, [tuple(row) for row in rows]
        finally:
            cursor.close()

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        database = getattr(self, "_database", None)
        if connection is None:
            return
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("USE [master]")
                cursor.execute(f"ALTER DATABASE {_bracket_identifier(database)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
                cursor.execute(f"DROP DATABASE {_bracket_identifier(database)}")
            finally:
                cursor.close()
        except Exception:
            pass
        finally:
            try:
                connection.close()
            except Exception:
                pass


def _oracle_connection_info(url: str) -> dict[str, Any]:
    parsed = urlsplit(url.strip())
    query = {key.lower(): values[-1] for key, values in parse_qs(parsed.query).items() if values}
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname or "127.0.0.1"
    service = query.get("service_name") or (parsed.path or "").strip("/")
    if not user or not service:
        raise OracleInputError("Oracle URL requires user and service name")
    dsn = query.get("dsn") or f"{host}:{parsed.port or 1521}/{service}"
    return {"user": user, "password": password, "dsn": dsn, "mode": query.get("mode", "").lower()}


def _oracle_password(value: str) -> str:
    # Oracle's quoted password syntax uses a quoted identifier, not a string
    # literal. This is safe for the generated random password.
    return '"' + value.replace('\"', '\"\"') + '"'


class _OracleRunner(_NativeRunner):
    """Independent Oracle world runner using a temporary owner account."""

    def __init__(self, admin: Any, info: dict[str, Any], owner: str, owner_password: str, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> None:
        self._admin = admin
        self._info = info
        self._owner = owner
        self._owner_password = owner_password
        self._owner_connection: Any = None
        try:
            self._materialize(tables, rows)
        except Exception:
            self.close()
            raise

    @classmethod
    def from_url(cls, url: str, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> "_OracleRunner":
        import oracledb

        info = _oracle_connection_info(url)
        connect_kwargs: dict[str, Any] = {"user": info["user"], "password": info["password"], "dsn": info["dsn"]}
        if info["mode"] == "sysdba" and hasattr(oracledb, "AUTH_MODE_SYSDBA"):
            connect_kwargs["mode"] = oracledb.AUTH_MODE_SYSDBA
        admin = oracledb.connect(**connect_kwargs)
        owner = f"GOLD_OWNER_{_world_token().upper()}"
        owner_password = f"G{_world_token()}xA9"
        cursor = admin.cursor()
        try:
            cursor.execute(f"CREATE USER {_oracle_identifier(owner)} IDENTIFIED BY {_oracle_password(owner_password)}")
            cursor.execute(f"GRANT CREATE SESSION, CREATE TABLE TO {_oracle_identifier(owner)}")
            cursor.execute(f"ALTER USER {_oracle_identifier(owner)} QUOTA 64M ON \"USERS\"")
        except Exception:
            try:
                cursor.execute(f"DROP USER {_oracle_identifier(owner)} CASCADE")
            except Exception:
                pass
            raise
        finally:
            cursor.close()
        return cls(admin, info, owner, owner_password, tables, rows)

    def _materialize(self, tables: Sequence["TableDef"], rows: dict[str, list[dict[str, Any]]]) -> None:
        import oracledb

        self._owner_connection = oracledb.connect(user=self._owner, password=self._owner_password, dsn=self._info["dsn"])
        cursor = self._owner_connection.cursor()
        try:
            if hasattr(self._owner_connection, "call_timeout"):
                self._owner_connection.call_timeout = 30_000
            for table in tables:
                for statement in _ddl_statements_for("oracle", table):
                    cursor.execute(statement)
                _insert_native_rows(cursor, "oracle", table, rows.get(table.name, []))
            self._owner_connection.commit()
        finally:
            cursor.close()

    def execute(self, sql: str, *, max_result_rows: int, max_vm_steps: int) -> tuple[list[str], list[tuple[Any, ...]]]:
        cursor = self._owner_connection.cursor()
        try:
            cursor.execute(sql)
            columns = [str(item[0]) for item in (cursor.description or ())]
            rows = cursor.fetchmany(max_result_rows + 1)
            if len(rows) > max_result_rows:
                raise OracleEngineGap("result row limit exceeded")
            return columns, [tuple(row) for row in rows]
        finally:
            cursor.close()

    def close(self) -> None:
        owner_connection = getattr(self, "_owner_connection", None)
        admin = getattr(self, "_admin", None)
        owner = getattr(self, "_owner", None)
        try:
            if owner_connection is not None:
                try:
                    owner_connection.rollback()
                finally:
                    owner_connection.close()
        except Exception:
            pass
        if admin is not None and owner:
            try:
                cursor = admin.cursor()
                try:
                    cursor.execute(f"DROP USER {_oracle_identifier(owner)} CASCADE")
                finally:
                    cursor.close()
            except Exception:
                pass
        if admin is not None:
            try:
                admin.close()
            except Exception:
                pass


def _insert_native_rows(cursor: Any, backend: str, table: "TableDef", rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = [column.name for column in table.columns]
    quote = _bracket_identifier if backend == "tsql" else _oracle_identifier
    placeholders = (",".join("?" for _ in columns) if backend == "tsql"
                   else ",".join(f":{index}" for index in range(1, len(columns) + 1)))
    statement = (f"INSERT INTO {quote(table.name)} (" + ",".join(quote(column) for column in columns)
                + f") VALUES ({placeholders})")
    payload = [[_coerce_python_value(_fit_native_value(table.columns[index], row[column])) for index, column in enumerate(columns)] for row in rows]
    cursor.executemany(statement, payload)


def _world_token() -> str:
    """A short unique suffix for the per-world schema name."""
    token = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    return token


def _parse_db_url(url: str, *, default_port: int) -> dict[str, Any]:
    """Parse ``mysql://user:pass@host:port/db`` style URLs defensively."""
    match = re.match(r"^[a-z+]+://(?P<user>[^:@/]+)(?::(?P<password>[^@/]*))?@(?P<host>[^:/]+)(?::(?P<port>\d+))?(?:/(?P<database>[^?]+)?)?", url)
    if not match:
        raise OracleEngineGap(f"unparseable native engine URL: {url!r}")
    password = unquote(match.group("password") or "")
    port_text = match.group("port")
    port = int(port_text) if port_text else default_port
    database = (match.group("database") or "").strip("/") or None
    return {
        "host": match.group("host"),
        "port": port,
        "user": unquote(match.group("user")),
        "password": password,
        "database": database,
    }


def _result_digest(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    payload = json.dumps(
        {"columns": list(columns), "rows": [list(row) for row in rows]},
        ensure_ascii=False,
        sort_keys=True,
        default=repr,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compare_results(
    standard_columns: Sequence[str],
    standard_rows: Sequence[Sequence[Any]],
    student_columns: Sequence[str],
    student_rows: Sequence[Sequence[Any]],
    *,
    ordered: bool,
) -> bool:
    if len(standard_columns) != len(student_columns):
        return False
    # Output labels are presentation metadata in this teaching oracle.  The
    # production judge also compares projected values rather than rejecting a
    # semantically identical CTE/alias rewrite solely because SQLite derives a
    # different unaliased column name.
    standard = [tuple(row) for row in standard_rows]
    student = [tuple(row) for row in student_rows]
    if ordered:
        return standard == student
    return Counter(standard) == Counter(student)


def _normalize_expected(value: Any) -> str | None:
    text = _norm(value).replace("-", "_")
    if text in {"equivalent", "eq"}:
        return EQUIVALENT
    if text in {"not_equivalent", "not equivalent", "wrong", "neq"}:
        return NOT_EQUIVALENT
    return None


def _safe_rows(rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {
        table: [dict(row) for row in values[:MAX_ROWS_PER_TABLE]]
        for table, values in rows.items()
    }


def run_gold_oracle(
    schema: str | None,
    standard_sql: str,
    student_sql: str,
    *,
    schema_catalog: Any = None,
    dialect: str | None = None,
    expected: str | None = None,
    seeds: Iterable[int] = (0, 1, 2),
    row_scales: Iterable[int] = (4, 8),
    max_rows_per_table: int = MAX_ROWS_PER_TABLE,
    max_result_rows: int = MAX_RESULT_ROWS,
    max_vm_steps: int = MAX_VM_STEPS,
) -> dict[str, Any]:
    """Run bounded independent worlds and return an auditable oracle result."""
    normalized_dialect = _norm(dialect) or "generic"
    if normalized_dialect not in SUPPORTED_DIALECTS and normalized_dialect not in NATIVE_DIALECTS:
        return {
            "verdict": ENGINE_GAP,
            "status": ENGINE_GAP,
            "equivalence_conclusion": UNDECIDED,
            "reason": f"no native runner configured for dialect {dialect!r}",
            "trials": [],
        }
    # A vendor dialect with no reachable native URL is an ENGINE_GAP rather than
    # a silent SQLite fallback, so the pair is honestly out-of-scope.
    if normalized_dialect in NATIVE_DIALECTS and not _native_dialect_url(normalized_dialect):
        return {
            "verdict": ENGINE_GAP,
            "status": ENGINE_GAP,
            "equivalence_conclusion": UNDECIDED,
            "reason": f"no native URL configured for dialect {dialect!r}",
            "trials": [],
        }
    if not isinstance(standard_sql, str) or not standard_sql.strip() or not isinstance(student_sql, str) or not student_sql.strip():
        return {
            "verdict": INPUT_GAP,
            "status": INPUT_GAP,
            "equivalence_conclusion": UNDECIDED,
            "reason": "both standard_sql and student_sql are required",
            "trials": [],
        }
    try:
        if schema_catalog is None and (not isinstance(schema, str) or not schema.strip()):
            if re.search(r"(?is)\b(?:from|join)\b", standard_sql) or re.search(
                r"(?is)\b(?:from|join)\b", student_sql
            ):
                raise OracleInputError("schema is required for table-referencing queries")
            tables = []
        else:
            tables = parse_schema(schema, schema_catalog)
    except (OracleInputError, TypeError, ValueError) as exc:
        return {
            "verdict": INPUT_GAP,
            "status": INPUT_GAP,
            "equivalence_conclusion": UNDECIDED,
            "reason": str(exc),
            "trials": [],
        }
    tables = _promote_numeric_columns(tables, (standard_sql, student_sql))
    expected_verdict = _normalize_expected(expected)
    trials: list[dict[str, Any]] = []
    ordered = bool(re.search(r"(?is)\border\s+by\b", standard_sql) or re.search(r"(?is)\border\s+by\b", student_sql))
    scales = [max(1, min(int(scale), max_rows_per_table, MAX_ROWS_PER_TABLE)) for scale in row_scales]
    seeds_list = [int(seed) for seed in seeds]
    if not scales or not seeds_list:
        return {
            "verdict": INPUT_GAP,
            "status": INPUT_GAP,
            "equivalence_conclusion": UNDECIDED,
            "reason": "at least one seed and row scale are required",
            "trials": [],
        }
    try:
        for seed in seeds_list:
            for scale in scales:
                for duplicate_rows in (False, True):
                    for layout in ("sliding", "aligned"):
                        rows = _generate_rows(tables, scale, seed, duplicate_rows=duplicate_rows)
                        _apply_query_boundaries(rows, tables, (standard_sql, student_sql))
                        _apply_query_literals(
                            rows, tables, (standard_sql, student_sql), layout=layout
                        )
                        _apply_comparison_boundary_witness(
                            rows,
                            tables,
                            standard_sql,
                            student_sql,
                        )
                        _apply_aggregate_function_witness(
                            rows,
                            tables,
                            standard_sql,
                            student_sql,
                        )
                        _apply_subquery_membership_paths(
                            rows, tables, (standard_sql, student_sql)
                        )
                        _apply_count_null_paths(
                            rows, tables, (standard_sql, student_sql)
                        )
                        if layout == "aligned":
                            _apply_join_gaps(rows, tables, (standard_sql, student_sql))
                        with _native_runner(normalized_dialect, tables, rows) as runner:
                            # Generic/SQLite execution needs numeric-leading
                            # schema headers quoted; vendor runners receive
                            # the original dialect SQL unchanged.
                            executable_standard_sql = standard_sql
                            executable_student_sql = student_sql
                            if normalized_dialect in {None, "", "generic", "standard", "sqlite"}:
                                executable_standard_sql = _quote_numeric_schema_identifiers(
                                    standard_sql, tables
                                )
                                executable_student_sql = _quote_numeric_schema_identifiers(
                                    student_sql, tables
                                )
                            try:
                                standard_columns, standard_rows = runner.execute(
                                    executable_standard_sql,
                                    max_result_rows=max_result_rows,
                                    max_vm_steps=max_vm_steps,
                                )
                            except Exception as exc:
                                schema_error = _native_schema_resolution_kind(
                                    exc, normalized_dialect
                                )
                                if schema_error is not None:
                                    raise OracleInputError(
                                        "standard query cannot resolve replayed schema object: "
                                        + schema_error
                                    ) from exc
                                raise
                            try:
                                student_columns, student_rows = runner.execute(
                                    executable_student_sql,
                                    max_result_rows=max_result_rows,
                                    max_vm_steps=max_vm_steps,
                                )
                            except Exception as exc:
                                schema_error = _native_schema_resolution_kind(
                                    exc, normalized_dialect
                                )
                                if schema_error is not None:
                                    return {
                                        "verdict": NOT_EQUIVALENT,
                                        "status": "SUPPORTED",
                                        "equivalence_conclusion": NOT_EQUIVALENT,
                                        "expected": expected_verdict,
                                        "reason": (
                                            "student query cannot resolve replayed schema object: "
                                            + schema_error
                                        ),
                                        "trials": trials,
                                        "distinguishing_world_id": (
                                            f"gold_{seed}_{scale}_{'duplicated' if duplicate_rows else 'varied'}_"
                                            f"{layout}_student_schema_resolution"
                                        ),
                                    }
                                raise
                        same = _compare_results(
                            standard_columns, standard_rows, student_columns, student_rows, ordered=ordered
                        )
                        flavour = "duplicated" if duplicate_rows else "varied"
                        trials.append({
                            "world_id": f"gold_{seed}_{scale}_{flavour}_{layout}",
                            "seed": seed,
                            "row_scale": scale,
                            "row_flavour": flavour,
                            "literal_layout": layout,
                            "database": _safe_rows(rows),
                            "standard_columns": standard_columns,
                            "student_columns": student_columns,
                            "standard_rows": [list(row) for row in standard_rows[:32]],
                            "student_rows": [list(row) for row in student_rows[:32]],
                            "standard_digest": _result_digest(standard_columns, standard_rows),
                            "student_digest": _result_digest(student_columns, student_rows),
                            "same_result": same,
                            "ordered_compare": ordered,
                            "execution_backend": normalized_dialect,
                            "native_adapter": runner.metadata(),
                        })
                        if not same:
                            return {
                                "verdict": NOT_EQUIVALENT,
                                "status": "SUPPORTED",
                                "equivalence_conclusion": NOT_EQUIVALENT,
                                "expected": expected_verdict,
                                "trials": trials,
                                "distinguishing_world_id": trials[-1]["world_id"],
                            }
    except OracleEngineGap as exc:
        return {
            "verdict": ENGINE_GAP,
            "status": ENGINE_GAP,
            "equivalence_conclusion": UNDECIDED,
            "expected": expected_verdict,
            "reason": str(exc),
            "trials": trials,
        }
    except (sqlite3.DatabaseError, OracleInputError) as exc:
        # A compact schema with duplicate physical column names cannot be
        # materialized without changing SQL name-resolution semantics. This is
        # an input/schema gap, not an unavailable execution engine.
        error_text = str(exc)
        schema_error = _native_schema_resolution_kind(exc, normalized_dialect)
        verdict = (
            INPUT_GAP
            if (
                re.search(r"(?i)duplicate column name", error_text)
                or "replayed schema object" in error_text
                or schema_error is not None
            )
            else ENGINE_GAP
        )
        return {
            "verdict": verdict,
            "status": verdict,
            "equivalence_conclusion": UNDECIDED,
            "expected": expected_verdict,
            "reason": error_text,
            "trials": trials,
        }
    except Exception as exc:  # noqa: BLE001 - classify only known native boundaries.
        schema_error = _native_schema_resolution_kind(exc, normalized_dialect)
        if schema_error is not None:
            return {
                "verdict": INPUT_GAP,
                "status": INPUT_GAP,
                "equivalence_conclusion": UNDECIDED,
                "expected": expected_verdict,
                "reason": "native schema replay failed: " + schema_error,
                "trials": trials,
            }
        native_modules = ("pymysql", "psycopg", "psycopg2", "pyodbc", "oracledb")
        if type(exc).__module__.startswith(native_modules):
            return {
                "verdict": ENGINE_GAP,
                "status": ENGINE_GAP,
                "equivalence_conclusion": UNDECIDED,
                "expected": expected_verdict,
                "reason": f"{type(exc).__name__}: {exc}",
                "trials": trials,
            }
        raise
    verdict = EQUIVALENT if expected_verdict == EQUIVALENT else UNDECIDED
    return {
        "verdict": verdict,
        "status": "SUPPORTED" if verdict == EQUIVALENT else UNDECIDED,
        "equivalence_conclusion": verdict,
        "expected": expected_verdict,
        "reason": "all bounded worlds matched" if verdict == EQUIVALENT else "no distinguishing bounded world found",
        "trials": trials,
    }


__all__ = [
    "ENGINE_GAP",
    "EQUIVALENT",
    "INPUT_GAP",
    "NOT_EQUIVALENT",
    "UNDECIDED",
    "parse_schema",
    "run_gold_oracle",
    "SUPPORTED_DIALECTS",
]
