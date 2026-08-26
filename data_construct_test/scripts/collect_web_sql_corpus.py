"""Collect and normalize external SQL corpora for convergence benchmarks.

Raw downloads stay in an auditable cache.  Replayable records must carry a
source-provided schema or a catalog parsed from source DDL/database metadata;
query-text inference is retained only for explicitly marked reference records.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import re
import sqlite3
import tarfile
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import sqlglot
from sqlglot import ErrorLevel, exp

try:
    from spider_schema_catalog import compact_schema, load_spider_catalog
except ModuleNotFoundError:  # Imported by tests from the repository root.
    import sys

    _SCRIPT_DIR = Path(__file__).resolve().parent
    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    from spider_schema_catalog import compact_schema, load_spider_catalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "data_construct_test" / "sources" / "web_sql_corpus_manifest.json"
OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
DEFAULT_CACHE_DIR = OUTPUT_DIR / "web_sql_raw"
DEFAULT_OUTPUT = OUTPUT_DIR / "web_sql_corpus.jsonl"
DEFAULT_REPORT = OUTPUT_DIR / "web_sql_corpus_report.json"

AGGREGATORS = ("", "MAX", "MIN", "COUNT", "SUM", "AVG")
WIKISQL_COND_OPS = ("=", ">", "<", "OP")
MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024

DIALECT_ALIASES = {
    "generic": None,
    "postgresql": "postgres",
    "sqlserver": "tsql",
    "sqlite3": "sqlite",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and normalize external SQL corpora.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--max-per-source", type=int, default=5000)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--local-file", action="append", type=Path, default=[])
    parser.add_argument("--offline-cache-only", action="store_true")
    parser.add_argument("--include-reference-only", action="store_true")
    parser.add_argument(
        "--spider-tables-json",
        type=Path,
        help="official Spider tables.json; required for Spider sources",
    )
    return parser.parse_args()


def _slug(value: str) -> str:
    slug = re.sub(r"\W+", "_", value.strip().lower()).strip("_")
    return slug or "value"


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _normalize_sql(sql: str) -> str:
    sql = re.sub(r"--.*?$", " ", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"\s+", " ", sql.strip())
    return sql.rstrip(";")


def _is_read_only_query(sql: str) -> bool:
    """Keep the equivalence corpus query-only and side-effect free."""

    normalized = _normalize_sql(sql)
    if not re.match(r"^(?:WITH|SELECT)\b", normalized, flags=re.IGNORECASE):
        return False
    if re.search(r"\b(?:INSERT|UPDATE|DELETE|MERGE|REPLACE|CREATE|ALTER|DROP|TRUNCATE)\b", normalized, re.IGNORECASE):
        return False
    if re.search(r"\bSELECT\s+.+?\bINTO\b", normalized, re.IGNORECASE | re.DOTALL):
        return False
    # A second WITH in the normalized text is a strong signal that a mined
    # corpus concatenated independent semicolon-free CTE examples. Nested CTEs
    # still use one leading WITH with comma-separated definitions.
    if len(re.findall(r"\bWITH\b", normalized, re.IGNORECASE)) > 1:
        return False
    return True


def _split_tutorial_sql(text: str) -> list[str]:
    """Split semicolon-free tutorial files at top-level CTE boundaries.

    Many teaching repositories put several independent ``WITH`` examples in a
    single file without semicolons. Treating the whole file as one statement
    can feed UPDATE/DELETE examples and multiple queries into the data
    generator, causing unbounded work. A line-start WITH is a reliable boundary
    for these files because nested SELECTs remain inside the current CTE.
    """

    starts = [match.start() for match in re.finditer(r"(?im)^WITH\b", text)]
    if len(starts) <= 1:
        return [text]
    starts.append(len(text))
    return [text[starts[index]:starts[index + 1]] for index in range(len(starts) - 1)]


def _extract_markdown_sql(text: str) -> Iterable[dict[str, Any]]:
    """Extract executable queries only from explicitly SQL-labelled fences."""

    for match in re.finditer(
        r"(?is)```(?:sql|postgres(?:ql)?|mysql|sqlite|tsql|sqlserver)\s*\n(.*?)```",
        text,
    ):
        yield from _extract_sql_text(match.group(1))


def _extract_pgexercises_queries(text: str) -> Iterable[dict[str, Any]]:
    """Read the canonical answer field from one pgExercises ``.ex`` file."""

    match = re.search(r"(?ms)^\|QUERY\|\s*\n(.*?)(?=^\|[A-Z]+\|\s*$)", text)
    if match is None:
        return
    sql = _normalize_sql(match.group(1))
    if sql and _is_read_only_query(sql):
        yield {"sql": sql}


def _extract_numbered_query_sections(text: str) -> Iterable[dict[str, Any]]:
    """Split answer sheets whose only reliable delimiter is a numbered comment."""

    starts = [
        match.start()
        for match in re.finditer(
            r"(?m)^--\s*(?:\d+(?:\.\d+)+|\d+\))(?=\s|$)", text
        )
    ]
    if not starts:
        yield from _extract_sql_text(text)
        return
    starts.append(len(text))
    for index in range(len(starts) - 1):
        yield from _extract_sql_text(text[starts[index]:starts[index + 1]])


def _labels(sql: str) -> list[str]:
    text = sql.lower()
    labels: set[str] = {"select-basic"}
    checks = [
        ("distinct", "distinct"),
        (" join ", "join-inner"),
        (" left join ", "join-left"),
        (" right join ", "join-right-full"),
        (" full join ", "join-right-full"),
        (" on ", "join-on"),
        (" group by ", "group-by"),
        (" having ", "having"),
        (" order by ", "order-by"),
        (" limit ", "limit-offset"),
        (" fetch first ", "limit-offset"),
        (" union ", "union"),
        (" intersect ", "intersect"),
        (" except ", "except"),
        (" over ", "window-agg"),
        (" with ", "cte"),
        (" recursive ", "cte-recursive"),
        (" is null", "null-handling"),
        (" coalesce", "null-handling"),
        (" between ", "between"),
        (" in ", "in-list"),
        (" like ", "like"),
        (" case ", "case"),
    ]
    for needle, label in checks:
        if needle in text:
            labels.add(label)
    if any(token in text for token in (" count(", " sum(", " avg(", " min(", " max(")):
        labels.add("agg-count")
    if re.search(r"[<>=]", text):
        labels.add("where-comp")
    if " where " in text:
        labels.add("where")
    if "select" in text and re.search(r"[+\-*/%]", text):
        labels.add("arithmetic")
    if " exists " in text:
        labels.add("subquery-exists")
    if re.search(r"\(\s*select\b", text):
        labels.add("subquery-scalar")
    return sorted(labels)


def _infer_schema(sql: str) -> str:
    tables: list[str] = []
    for match in re.finditer(
        r"\bfrom\s+([A-Za-z_][\w$]*)|\bjoin\s+([A-Za-z_][\w$]*)",
        sql,
        flags=re.IGNORECASE,
    ):
        table = match.group(1) or match.group(2)
        if table:
            tables.append(_slug(table))
    tables = list(dict.fromkeys(tables))
    if not tables:
        return ""

    columns = [
        _slug(value)
        for value in re.findall(r"(?<!['\"])\b([A-Za-z_][\w$]*)\b(?!['\"])", sql)
        if value.lower() not in {
            "select", "from", "where", "join", "left", "right", "full", "inner", "outer",
            "on", "and", "or", "not", "null", "is", "in", "exists", "group", "by",
            "having", "order", "limit", "offset", "with", "as", "case", "when", "then",
            "else", "end", "distinct", "union", "all", "intersect", "except", "over",
            "partition", "rows", "range", "between", "like", "true", "false",
        }
    ]
    cols = [col for col in dict.fromkeys(columns) if col not in tables]
    if not cols:
        cols = ["id", "value"]
    return "; ".join(f"{table}({', '.join(cols[:12])})" for table in tables) + ";"


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("sources", []))


def _cache_path(cache_dir: Path, source: dict[str, Any]) -> Path:
    url = str(source.get("url") or source["id"])
    suffix = Path(url.split("?", 1)[0]).suffix
    if url.endswith(".tar.bz2"):
        suffix = ".tar.bz2"
    elif url.endswith(".tar.gz"):
        suffix = ".tar.gz"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return cache_dir / f"{source['id']}__{digest}{suffix or '.dat'}"


def _download(source: dict[str, Any], cache_dir: Path, timeout: int, offline: bool) -> Path | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, source)
    if path.exists() and path.stat().st_size:
        return path
    if offline:
        return None
    request = urllib.request.Request(
        str(source["url"]),
        headers={"User-Agent": "sql-edu-corpus-collector/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        path.write_bytes(response.read())
    return path


def _download_url(url: str, path: Path, timeout: int, offline: bool) -> Path | None:
    """Download one explicitly addressed page into the auditable cache."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        return path
    if offline:
        return None
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "sql-edu-corpus-collector/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        path.write_bytes(response.read())
    return path


def _extract_sql_text(text: str) -> Iterable[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if payload is not None:
        yield from _extract_from_json(payload)
        return
    parsed_json_lines = False
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield from _extract_from_json(json.loads(line))
            parsed_json_lines = True
        except json.JSONDecodeError:
            pass
    # JSONL documents have already been fully traversed.  Falling through to
    # the broad SELECT/semicolon regex would double every row (and could
    # silently hit max_items before the source's tail was seen).
    if parsed_json_lines:
        return
    # Search only executable text.  Matching before comment removal turns
    # prose such as ``-- Select the warehouses ...`` or ``/* With a
    # subquery */`` into a malformed SQL prefix.
    executable_text = re.sub(r"--.*?$", " ", text, flags=re.MULTILINE)
    executable_text = re.sub(r"/\*.*?\*/", " ", executable_text, flags=re.DOTALL)
    for document in _split_tutorial_sql(executable_text):
        for match in re.finditer(r"\b(?:WITH|SELECT)\b.+?(?:;|$)", document, flags=re.IGNORECASE | re.DOTALL):
            sql = _normalize_sql(match.group(0))
            if sql and _is_read_only_query(sql):
                yield {"sql": sql}


def _extract_from_json(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        if payload.get("cell_type") == "code":
            source = payload.get("source")
            if isinstance(source, list):
                source = "".join(str(part) for part in source)
            if isinstance(source, str):
                source = re.sub(r"(?im)^\s*%%sql[^\n]*\n?", "", source)
                yield from _extract_sql_text(source)
        for key in ("query", "SQL", "sql", "gold", "ans_sql", "answer_sql"):
            value = payload.get(key)
            if isinstance(value, str) and _is_read_only_query(value):
                yield {
                    "sql": _normalize_sql(value),
                    "schema": payload.get("schema") or payload.get("db_schema") or "",
                    "schema_catalog": payload.get("schema_catalog"),
                    "db_id": payload.get("db_id") or payload.get("database_id") or payload.get("table_id"),
                }
        for value in payload.values():
            yield from _extract_from_json(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _extract_from_json(value)


def _iter_archive_members(path: Path) -> Iterable[tuple[str, bytes]]:
    mode = "r:*"
    with tarfile.open(path, mode) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            lower = member.name.lower()
            if not lower.endswith((".json", ".jsonl", ".sql", ".txt", ".md", ".ex", ".ipynb")):
                continue
            if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            yield member.name, extracted.read()


def _iter_raw_documents(path: Path) -> Iterable[tuple[str, bytes]]:
    lower = path.name.lower()
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
        yield from _iter_archive_members(path)
    else:
        yield path.name, path.read_bytes()


def _collect_wikisql(path: Path, source: dict[str, Any], max_items: int) -> list[dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = {}
    examples: list[tuple[str, dict[str, Any]]] = []
    for member_name, raw in _iter_raw_documents(path):
        if not member_name.endswith(".jsonl"):
            continue
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "header" in item and "id" in item:
                tables[str(item["id"])] = item
            elif isinstance(item.get("sql"), dict):
                examples.append((member_name, item))

    records: list[dict[str, Any]] = []
    for member_name, item in examples:
        table = tables.get(str(item.get("table_id")))
        if not table:
            continue
        headers = [_slug(str(header)) for header in table.get("header", [])]
        if not headers:
            continue
        table_name = f"wikisql_{_slug(str(item.get('table_id')))}"
        sql_obj = item["sql"]
        sel_index = int(sql_obj.get("sel", 0))
        agg_index = int(sql_obj.get("agg", 0))
        if sel_index >= len(headers):
            continue
        select_expr = headers[sel_index]
        agg = AGGREGATORS[agg_index] if 0 <= agg_index < len(AGGREGATORS) else ""
        if agg:
            select_expr = f"{agg}({select_expr})"
        predicates = []
        for condition in sql_obj.get("conds", []):
            if not isinstance(condition, list) or len(condition) < 3:
                continue
            col_index, op_index, value = condition[:3]
            if not isinstance(col_index, int) or col_index >= len(headers):
                continue
            op = WIKISQL_COND_OPS[op_index] if isinstance(op_index, int) and 0 <= op_index < len(WIKISQL_COND_OPS) else "="
            if op == "OP":
                op = "="
            predicates.append(f"{headers[col_index]} {op} {_sql_literal(value)}")
        sql = f"SELECT {select_expr} FROM {table_name}"
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        schema = f"{table_name}({', '.join(headers)});"
        records.append(_record(
            source,
            sql,
            schema,
            member_name,
            "wikisql_structured",
            schema_trust="source_structured",
            replay_eligible=True,
        ))
        if len(records) >= max_items:
            break
    return records


def _sqlglot_dialect(dialect: Any) -> str | None:
    normalized = str(dialect or "").strip().lower()
    return DIALECT_ALIASES.get(normalized, normalized or None)


def _detected_query_dialect(sql: str, default: Any, *, mixed: bool) -> str | None:
    dialect = _sqlglot_dialect(default)
    if not mixed:
        return dialect
    if re.search(r"(?is)^\s*SELECT\s+TOP\s+(?:\(|\d)", sql):
        return "tsql"
    if re.search(r"(?is)\bSYS\s*\.\s*ALL_INDEXES\b", sql):
        return "oracle"
    if re.search(r"(?is)\bSQLITE_MASTER\b", sql):
        return "sqlite"
    return dialect


def _strict_query_ast(sql: str, dialect: str | None) -> exp.Query | None:
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
    except Exception:
        return None
    parsed = [
        statement
        for statement in statements
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]
    return parsed[0] if len(parsed) == 1 and isinstance(parsed[0], exp.Query) else None


def _catalog_query_compatibility(
    ast: exp.Query,
    catalog: dict[str, Any],
) -> tuple[bool, list[str]]:
    catalog_tables = {
        str(table.get("name") or "").strip().lower()
        for table in catalog.get("tables") or ()
    }
    cte_names = {
        str(cte.alias_or_name or "").strip().lower()
        for cte in ast.find_all(exp.CTE)
        if cte.alias_or_name
    }
    missing = sorted({
        str(table.name or "").strip().lower()
        for table in ast.find_all(exp.Table)
        if table.name
        and str(table.name).strip().lower() not in cte_names
        and str(table.name).strip().lower() not in catalog_tables
    })
    return not missing, missing


def _constraint_expressions(expression: exp.Expression) -> Iterable[exp.Expression]:
    if isinstance(expression, exp.Constraint):
        for nested in expression.expressions:
            yield from _constraint_expressions(nested)
        return
    yield expression


def _identifier_names(expressions: Iterable[exp.Expression]) -> list[str]:
    names: list[str] = []
    for expression in expressions:
        name = str(getattr(expression, "name", "") or "").strip()
        if name and name.lower() not in {item.lower() for item in names}:
            names.append(name)
    return names


def _reference_target(reference: exp.Expression | None) -> tuple[str, list[str]] | None:
    if not isinstance(reference, exp.Reference):
        return None
    target = reference.this
    if isinstance(target, exp.Schema):
        table = target.this
        columns = _identifier_names(target.expressions)
    else:
        table = target
        columns = []
    if not isinstance(table, exp.Table) or not table.name:
        return None
    return str(table.name), columns


def _empty_catalog_table(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "columns": [],
        "primary_key": [],
        "foreign_keys": [],
        "unique_constraints": [],
    }


def _apply_table_constraint(table: dict[str, Any], constraint: exp.Expression) -> None:
    if isinstance(constraint, exp.PrimaryKey):
        columns = _identifier_names(constraint.expressions)
        if columns:
            table["primary_key"] = columns
        return
    if isinstance(constraint, exp.ForeignKey):
        source_columns = _identifier_names(constraint.expressions)
        target = _reference_target(constraint.args.get("reference"))
        if target is None or not source_columns or len(source_columns) != len(target[1]):
            return
        item = {
            "columns": source_columns,
            "references_table": target[0],
            "references_columns": target[1],
        }
        if item not in table["foreign_keys"]:
            table["foreign_keys"].append(item)
        return
    if isinstance(constraint, exp.UniqueColumnConstraint):
        columns = _identifier_names(constraint.expressions)
        if columns and columns not in table["unique_constraints"]:
            table["unique_constraints"].append(columns)


def _parse_ddl_catalog(
    ddl: str,
    *,
    dialect: Any,
    source_id: str,
    database_id: str,
) -> dict[str, Any]:
    """Parse physical table metadata from source DDL without executing it."""

    read_dialect = _sqlglot_dialect(dialect)
    statements = sqlglot.parse(
        ddl,
        read=read_dialect,
        error_level=ErrorLevel.IGNORE,
    )
    tables: dict[str, dict[str, Any]] = {}
    for statement in statements:
        if not isinstance(statement, exp.Create):
            continue
        if str(statement.args.get("kind") or "").upper() != "TABLE":
            continue
        schema = statement.this
        if not isinstance(schema, exp.Schema) or not isinstance(schema.this, exp.Table):
            continue
        table_name = str(schema.this.name or "").strip()
        if not table_name:
            continue
        table = tables.setdefault(table_name.lower(), _empty_catalog_table(table_name))
        for expression in schema.expressions:
            if isinstance(expression, exp.ColumnDef):
                column_name = str(expression.name or "").strip()
                if not column_name or any(
                    str(item.get("name") or "").lower() == column_name.lower()
                    for item in table["columns"]
                ):
                    continue
                data_type = expression.args.get("kind")
                constraints = list(expression.args.get("constraints") or ())
                kinds = [item.args.get("kind") for item in constraints]
                is_primary = any(
                    isinstance(kind, exp.PrimaryKeyColumnConstraint) for kind in kinds
                )
                is_unique = any(
                    isinstance(kind, exp.UniqueColumnConstraint) for kind in kinds
                )
                nullable = not is_primary and not any(
                    isinstance(kind, exp.NotNullColumnConstraint) for kind in kinds
                )
                generated = any(
                    "GENERATED" in type(kind).__name__.upper()
                    or "IDENTITY" in type(kind).__name__.upper()
                    or "COMPUTED" in type(kind).__name__.upper()
                    for kind in kinds
                    if kind is not None
                )
                table["columns"].append({
                    "name": column_name,
                    "data_type": (
                        data_type.sql(dialect=read_dialect)
                        if isinstance(data_type, exp.Expression) and read_dialect
                        else data_type.sql() if isinstance(data_type, exp.Expression) else "TEXT"
                    ),
                    "nullable": nullable,
                    "is_primary_key": is_primary,
                    "is_generated": generated,
                })
                if is_primary and column_name not in table["primary_key"]:
                    table["primary_key"].append(column_name)
                if is_unique and [column_name] not in table["unique_constraints"]:
                    table["unique_constraints"].append([column_name])
                for kind in kinds:
                    target = _reference_target(kind)
                    if target is None or len(target[1]) != 1:
                        continue
                    foreign_key = {
                        "columns": [column_name],
                        "references_table": target[0],
                        "references_columns": target[1],
                    }
                    if foreign_key not in table["foreign_keys"]:
                        table["foreign_keys"].append(foreign_key)
                continue
            for constraint in _constraint_expressions(expression):
                _apply_table_constraint(table, constraint)

    for statement in statements:
        if not isinstance(statement, exp.Alter) or not isinstance(statement.this, exp.Table):
            continue
        table = tables.get(str(statement.this.name or "").lower())
        if table is None:
            continue
        for action in statement.args.get("actions") or ():
            for constraint in action.find_all(exp.PrimaryKey, exp.ForeignKey, exp.UniqueColumnConstraint):
                _apply_table_constraint(table, constraint)

    usable = [table for table in tables.values() if table["columns"]]
    if not usable:
        raise ValueError(f"source DDL produced no physical tables: {source_id}")
    for table in usable:
        primary = {str(name).lower() for name in table["primary_key"]}
        for column in table["columns"]:
            if str(column["name"]).lower() in primary:
                column["is_primary_key"] = True
                column["nullable"] = False
        if table["primary_key"] and table["primary_key"] not in table["unique_constraints"]:
            table["unique_constraints"].append(list(table["primary_key"]))
    return {
        "source": "source_ddl",
        "source_id": source_id,
        "db_id": database_id,
        "tables": usable,
    }


def _sqlite_catalog(raw: bytes, *, source_id: str, database_id: str) -> dict[str, Any]:
    """Read an archived SQLite database catalog without materializing its rows."""

    connection = sqlite3.connect(":memory:")
    try:
        deserialize = getattr(connection, "deserialize", None)
        if deserialize is None:
            raise RuntimeError("this Python SQLite binding does not support deserialize()")
        deserialize(raw)
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables: list[dict[str, Any]] = []
        for table_name in table_names:
            quoted = '"' + table_name.replace('"', '""') + '"'
            column_rows = list(connection.execute(f"PRAGMA table_info({quoted})"))
            primary_key = [
                str(row[1])
                for row in sorted(column_rows, key=lambda item: int(item[5] or 0))
                if int(row[5] or 0) > 0
            ]
            columns = [{
                "name": str(row[1]),
                "data_type": str(row[2] or "TEXT"),
                "nullable": not bool(row[3]) and not bool(row[5]),
                "is_primary_key": bool(row[5]),
                "is_generated": False,
            } for row in column_rows]
            foreign_groups: dict[int, list[tuple[int, str, str, str]]] = {}
            for row in connection.execute(f"PRAGMA foreign_key_list({quoted})"):
                foreign_groups.setdefault(int(row[0]), []).append(
                    (int(row[1]), str(row[3]), str(row[2]), str(row[4]))
                )
            foreign_keys = []
            for entries in foreign_groups.values():
                ordered = sorted(entries)
                foreign_keys.append({
                    "columns": [item[1] for item in ordered],
                    "references_table": ordered[0][2],
                    "references_columns": [item[3] for item in ordered],
                })
            unique_constraints: list[list[str]] = [primary_key] if primary_key else []
            for index_row in connection.execute(f"PRAGMA index_list({quoted})"):
                if not bool(index_row[2]):
                    continue
                index_name = str(index_row[1])
                index_quoted = '"' + index_name.replace('"', '""') + '"'
                values = [
                    str(row[2])
                    for row in connection.execute(f"PRAGMA index_info({index_quoted})")
                    if row[2] is not None
                ]
                if values and values not in unique_constraints:
                    unique_constraints.append(values)
            if columns:
                tables.append({
                    "name": table_name,
                    "columns": columns,
                    "primary_key": primary_key,
                    "foreign_keys": foreign_keys,
                    "unique_constraints": unique_constraints,
                })
    finally:
        connection.close()
    if not tables:
        raise ValueError(f"SQLite source produced no physical tables: {source_id}")
    return {
        "source": "sqlite_database_metadata",
        "source_id": source_id,
        "db_id": database_id,
        "tables": tables,
    }


def _read_matching_archive_members(path: Path, patterns: list[str]) -> dict[str, bytes]:
    selected: dict[str, bytes] = {}
    with tarfile.open(path, "r:*") as archive:
        for member in archive.getmembers():
            if (
                not member.isfile()
                or member.size > MAX_ARCHIVE_MEMBER_BYTES
                or not any(fnmatch.fnmatch(member.name, pattern) for pattern in patterns)
            ):
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                selected[member.name] = extracted.read()
    return selected


def _nearest_catalog(
    member_name: str,
    catalogs: dict[str, tuple[dict[str, Any], list[str]]],
) -> tuple[dict[str, Any], list[str]] | None:
    parent = PurePosixPath(member_name).parent
    for candidate in (parent, *parent.parents):
        found = catalogs.get(str(candidate))
        if found is not None:
            return found
    return catalogs.get(".")


def _collect_archive_ddl_queries(
    path: Path,
    source: dict[str, Any],
    max_items: int,
) -> list[dict[str, Any]]:
    extraction = source.get("extraction") or {}
    query_patterns = [str(item) for item in extraction.get("query_members") or ()]
    schema_patterns = [str(item) for item in extraction.get("schema_members") or ()]
    sqlite_member = str(extraction.get("sqlite_schema_member") or "")
    if not query_patterns or (not schema_patterns and not sqlite_member):
        raise ValueError("archive_ddl_queries requires query members and a schema source")
    members = _read_matching_archive_members(
        path,
        [*query_patterns, *schema_patterns, *([sqlite_member] if sqlite_member else [])],
    )
    pairing = str(extraction.get("schema_pairing") or "global")
    catalogs: dict[str, tuple[dict[str, Any], list[str]]] = {}
    if sqlite_member:
        raw = members.get(sqlite_member)
        if raw is None:
            raise ValueError(f"SQLite schema member not found: {sqlite_member}")
        catalog = _sqlite_catalog(raw, source_id=source["id"], database_id=source["id"])
        catalogs["."] = (catalog, [sqlite_member])
    else:
        grouped: dict[str, list[tuple[str, str]]] = {}
        for member_name, raw in members.items():
            if not any(fnmatch.fnmatch(member_name, pattern) for pattern in schema_patterns):
                continue
            key = str(PurePosixPath(member_name).parent) if pairing == "directory" else "."
            grouped.setdefault(key, []).append(
                (member_name, raw.decode("utf-8", errors="replace"))
            )
        for key, documents in grouped.items():
            documents.sort()
            catalog = _parse_ddl_catalog(
                "\n".join(text for _, text in documents),
                dialect=source.get("dialect"),
                source_id=source["id"],
                database_id=f"{source['id']}:{key}",
            )
            catalogs[key] = (catalog, [name for name, _ in documents])

    query_format = str(extraction.get("query_format") or "sql")
    records: list[dict[str, Any]] = []
    for member_name in sorted(members):
        if not any(fnmatch.fnmatch(member_name, pattern) for pattern in query_patterns):
            continue
        resolved = _nearest_catalog(member_name, catalogs)
        if resolved is None:
            continue
        catalog, schema_members = resolved
        text = members[member_name].decode("utf-8", errors="replace")
        if query_format == "pgexercises":
            queries = _extract_pgexercises_queries(text)
        elif query_format == "numbered_comments":
            queries = _extract_numbered_query_sections(text)
        elif query_format == "markdown":
            queries = _extract_markdown_sql(text)
        else:
            queries = _extract_sql_text(text)
        for item in queries:
            sql = item.get("sql")
            if not isinstance(sql, str) or not _is_read_only_query(sql):
                continue
            query_dialect = _detected_query_dialect(
                sql,
                source.get("dialect"),
                mixed=bool(extraction.get("mixed_dialects")),
            )
            query_ast = _strict_query_ast(sql, query_dialect)
            compatible, missing_tables = (
                _catalog_query_compatibility(query_ast, catalog)
                if query_ast is not None
                else (False, [])
            )
            replay_eligible = query_ast is not None and compatible
            trust = (
                "authoritative_source_catalog"
                if replay_eligible
                else "catalog_query_parse_failed"
                if query_ast is None
                else "catalog_physical_table_mismatch"
            )
            record = _record(
                source,
                sql,
                compact_schema(catalog),
                member_name,
                "archive_ddl_queries",
                schema_catalog=catalog,
                schema_trust=trust,
                replay_eligible=replay_eligible,
                dialect=query_dialect,
            )
            record["schema_members"] = schema_members
            if not replay_eligible:
                record["admission_reason"] = (
                    "strict_query_parse_failed"
                    if query_ast is None
                    else "physical_table_not_in_source_catalog"
                )
                record["missing_catalog_tables"] = missing_tables
            records.append(record)
            if len(records) >= max_items:
                return records
    return records


def _record(
    source: dict[str, Any],
    sql: str,
    schema: str,
    member_name: str,
    method: str,
    *,
    schema_catalog: dict[str, Any] | None = None,
    schema_trust: str | None = None,
    replay_eligible: bool | None = None,
    dialect: str | None = None,
) -> dict[str, Any]:
    sql = _normalize_sql(sql)
    inferred = not schema and schema_catalog is None
    schema = schema or _infer_schema(sql)
    if schema_trust is None:
        schema_trust = (
            "authoritative_source_catalog"
            if schema_catalog is not None
            else "query_text_inferred" if inferred else "source_declared"
        )
    if replay_eligible is None:
        replay_eligible = schema_catalog is not None or (
            bool(schema) and schema_trust not in {"query_text_inferred", "unknown"}
        )
    digest = hashlib.sha256(f"{source['id']}\0{member_name}\0{sql}".encode("utf-8")).hexdigest()
    return {
        "id": f"websql_{digest[:20]}",
        "source_id": source["id"],
        "source_name": source.get("name") or source["id"],
        "source_kind": source.get("kind") or "unknown",
        "source_url": source.get("url"),
        "member": member_name,
        "extraction_method": method,
        "dialect": dialect or source.get("dialect") or "generic",
        "sql": sql,
        "schema": schema,
        "schema_catalog": schema_catalog,
        "schema_trust": schema_trust,
        "replay_eligible": bool(replay_eligible),
        "cfg_labels": _labels(sql),
        "provenance_hash": digest,
    }


def _collect_generic(
    path: Path,
    source: dict[str, Any],
    max_items: int,
    spider_catalog: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for member_name, raw in _iter_raw_documents(path):
        text = raw.decode("utf-8", errors="replace")
        if member_name.lower().endswith(".md"):
            extracted = _extract_markdown_sql(text)
        elif member_name.lower().endswith(".ex"):
            extracted = _extract_pgexercises_queries(text)
        else:
            extracted = _extract_sql_text(text)
        for item in extracted:
            sql = item.get("sql")
            if not isinstance(sql, str):
                continue
            db_id = str(item.get("db_id") or "").strip()
            catalog_entry = item.get("schema_catalog")
            if source["id"].startswith("spider_"):
                # Immutable local snapshots may already carry the compact
                # source schema.  Preserve it and the authoritative Spider
                # source id instead of requiring a second tables.json fetch.
                # API rows without a retained schema still require the
                # official catalog and remain fail-closed.
                declared_schema = str(item.get("schema") or "").strip()
                declared_trust = str(item.get("schema_trust") or "").strip()
                has_authoritative_snapshot = bool(item.get("schema_catalog")) or (
                    declared_trust == "authoritative_source_catalog"
                )
                if declared_schema and has_authoritative_snapshot:
                    schema = declared_schema
                else:
                    if spider_catalog is None:
                        raise ValueError(
                            "Spider source requires --spider-tables-json unless the row carries an authoritative schema catalog"
                        )
                    catalog_entry = spider_catalog.get(db_id.lower())
                    if catalog_entry is None:
                        raise ValueError(f"Spider db_id is missing from tables.json: {db_id!r}")
                    schema = compact_schema(catalog_entry)
            else:
                schema = str(item.get("schema") or "")
            configured_trust = str(
                (source.get("extraction") or {}).get("schema_trust") or ""
            ).strip() or None
            records.append(_record(
                source,
                sql,
                schema,
                member_name,
                "generic_recursive",
                schema_catalog=catalog_entry,
                schema_trust=configured_trust,
            ))
            if len(records) >= max_items:
                return records
    return records


def _collect_hf_rows_api(
    source: dict[str, Any],
    cache_dir: Path,
    timeout: int,
    offline: bool,
    max_items: int,
    spider_catalog: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Collect Hugging Face datasets-server rows without requiring `datasets`.

    The endpoint returns a bounded page of JSON rows.  We page deterministically
    and cache each page separately, which makes offline replay possible after a
    successful online collection.
    """

    parsed = urlsplit(str(source["url"]))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    page_size = max(1, min(int(source.get("page_size", 100)), 100))
    offset = int(query.get("offset", "0") or 0)
    records: list[dict[str, Any]] = []
    while len(records) < max_items:
        page_query = dict(query)
        page_query["offset"] = str(offset)
        page_query["length"] = str(min(page_size, max_items - len(records)))
        page_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(page_query), parsed.fragment))
        digest = hashlib.sha256(page_url.encode("utf-8")).hexdigest()[:16]
        page_path = cache_dir / f"{source['id']}__page_{offset:08d}_{digest}.json"
        cached = _download_url(page_url, page_path, timeout, offline)
        if cached is None:
            break
        try:
            payload = json.loads(cached.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            break
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        for wrapper in rows:
            item = wrapper.get("row") if isinstance(wrapper, dict) else None
            if not isinstance(item, dict):
                continue
            sql = item.get("query") or item.get("SQL") or item.get("sql")
            if not isinstance(sql, str) or not _is_read_only_query(sql):
                continue
            db_id = str(item.get("db_id") or "")
            if spider_catalog is None:
                raise ValueError(
                    "Spider source requires --spider-tables-json; query-text schema inference is disabled"
                )
            catalog_entry = spider_catalog.get(db_id.lower())
            if catalog_entry is None:
                raise ValueError(f"Spider db_id is missing from tables.json: {db_id!r}")
            records.append(_record(
                source,
                sql,
                compact_schema(catalog_entry),
                f"{query.get('split', 'split')}:{offset}",
                "hf_rows_api",
                schema_catalog=catalog_entry,
            ))
            if len(records) >= max_items:
                break
        offset += len(rows)
        if len(rows) < page_size:
            break
    return records


def collect_source(
    source: dict[str, Any],
    cache_dir: Path,
    timeout: int,
    offline: bool,
    max_items: int,
    spider_catalog: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    mode = (source.get("extraction") or {}).get("mode")
    if mode == "hf_rows_api":
        return _collect_hf_rows_api(source, cache_dir, timeout, offline, max_items, spider_catalog)
    path = source.get("local_path")
    if path:
        raw_path = Path(path)
        if not raw_path.is_absolute():
            raw_path = PROJECT_ROOT / raw_path
    else:
        downloaded = _download(source, cache_dir, timeout, offline)
        if downloaded is None:
            return []
        raw_path = downloaded
    if mode == "wikisql_structured":
        return _collect_wikisql(raw_path, source, max_items)
    if mode == "archive_ddl_queries":
        return _collect_archive_ddl_queries(raw_path, source, max_items)
    if mode == "reference_only":
        return []
    return _collect_generic(raw_path, source, max_items, spider_catalog)


def main() -> None:
    args = parse_args()
    if args.max_per_source <= 0:
        raise SystemExit("--max-per-source must be > 0")
    spider_catalog = (
        load_spider_catalog(args.spider_tables_json)
        if args.spider_tables_json is not None
        else None
    )
    sources = _read_manifest(args.manifest)
    selected = set(args.source_id)
    if args.local_file:
        for index, local_file in enumerate(args.local_file, start=1):
            sources.append({
                "id": f"local_file_{index}_{_slug(local_file.stem)}",
                "name": str(local_file),
                "kind": "local_external_seed",
                "enabled": True,
                "reference_only": False,
                "local_path": str(local_file),
                "dialect": "generic",
                "extraction": {"mode": "json_recursive"},
            })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    all_records: list[dict[str, Any]] = []
    source_stats: dict[str, Any] = {}
    for source in sources:
        if selected and source["id"] not in selected:
            continue
        if not source.get("enabled") and not selected:
            continue
        if source.get("reference_only") and not args.include_reference_only:
            source_stats[source["id"]] = {"status": "reference_only_skipped", "records": 0}
            continue
        try:
            records = collect_source(
                source,
                args.cache_dir,
                args.timeout,
                args.offline_cache_only,
                args.max_per_source,
                spider_catalog,
            )
        except Exception as exc:  # noqa: BLE001 - report source-level failures without losing other corpora.
            source_stats[source["id"]] = {"status": "error", "records": 0, "error": str(exc)}
            continue
        replayable = [record for record in records if record.get("replay_eligible")]
        reference_only = len(records) - len(replayable)
        admitted = records if args.include_reference_only else replayable
        source_stats[source["id"]] = {
            "status": "ok",
            "records": len(admitted),
            "replayable_records": len(replayable),
            "reference_only_records": reference_only,
        }
        all_records.extend(admitted)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for record in all_records:
        key = hashlib.sha256(_normalize_sql(record["sql"]).lower().encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)

    args.output.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in deduped) + ("\n" if deduped else ""),
        encoding="utf-8",
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest),
        "output": str(args.output),
        "total_records": len(deduped),
        "raw_records_before_dedupe": len(all_records),
        "source_stats": source_stats,
        "label_counts": dict(Counter(label for record in deduped for label in record["cfg_labels"])),
        "source_counts": dict(Counter(record["source_id"] for record in deduped)),
        "schema_trust_counts": dict(Counter(record["schema_trust"] for record in deduped)),
        "replayable_records": sum(bool(record.get("replay_eligible")) for record in deduped),
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
