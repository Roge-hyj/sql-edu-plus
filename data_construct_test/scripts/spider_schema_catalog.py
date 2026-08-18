"""Load Spider ``tables.json`` into an auditable physical-schema catalog.

Spider SQL must never be paired with a schema guessed from its query text.
This module preserves the original table/column spelling, SQLite-compatible
type hints, primary keys and foreign keys so corpus preparation can attach the
actual database contract to every replayable record.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _sql_type(spider_type: Any) -> str:
    normalized = str(spider_type or "").strip().lower()
    return {
        "number": "BIGINT",
        "text": "TEXT",
        "time": "DATETIME",
        "boolean": "BOOLEAN",
        "others": "TEXT",
    }.get(normalized, "TEXT")


def _identifier(value: Any) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", raw):
        return raw
    return "[" + raw.replace("]", "]]" ) + "]"


def _column_reference(
    columns: list[tuple[int, str]],
    table_names: list[str],
    index: Any,
) -> tuple[str, str] | None:
    if not isinstance(index, int) or index < 0 or index >= len(columns):
        return None
    table_index, column = columns[index]
    if table_index < 0 or table_index >= len(table_names) or not column:
        return None
    return table_names[table_index], column


def normalize_spider_schema(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalize one standard Spider ``tables.json`` object.

    The returned object contains only JSON primitives and is safe to embed in
    JSONL corpus records.  ``nullable`` remains ``None`` when Spider has no
    nullability metadata; inferring ``NOT NULL`` would make generated witness
    databases stricter than the source database.
    """
    db_id = str(entry.get("db_id") or "").strip()
    table_names = [str(item or "").strip() for item in entry.get("table_names_original") or ()]
    raw_columns = list(entry.get("column_names_original") or ())
    raw_types = list(entry.get("column_types") or ())
    columns: list[tuple[int, str]] = []
    for raw in raw_columns:
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try:
                table_index = int(raw[0])
            except (TypeError, ValueError):
                table_index = -1
            columns.append((table_index, str(raw[1] or "").strip()))
        else:
            columns.append((-1, ""))

    primary_indexes = {
        item for item in entry.get("primary_keys") or ()
        if isinstance(item, int)
    }
    foreign_keys: dict[tuple[str, str], list[dict[str, str]]] = {}
    for raw in entry.get("foreign_keys") or ():
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            continue
        source = _column_reference(columns, table_names, raw[0])
        target = _column_reference(columns, table_names, raw[1])
        if source is None or target is None:
            continue
        foreign_keys.setdefault(source, []).append({
            "table": target[0],
            "column": target[1],
        })

    tables: list[dict[str, Any]] = []
    for table_index, table_name in enumerate(table_names):
        if not table_name:
            continue
        table_columns: list[dict[str, Any]] = []
        primary_key: list[str] = []
        table_foreign_keys: list[dict[str, str]] = []
        for column_index, (owner, column_name) in enumerate(columns):
            if owner != table_index or not column_name:
                continue
            is_primary = column_index in primary_indexes
            table_columns.append({
                "name": column_name,
                "data_type": _sql_type(raw_types[column_index] if column_index < len(raw_types) else None),
                "nullable": None,
                "is_primary_key": is_primary,
            })
            if is_primary:
                primary_key.append(column_name)
            for target in foreign_keys.get((table_name, column_name), ()):
                table_foreign_keys.append({
                    "column": column_name,
                    "references_table": target["table"],
                    "references_column": target["column"],
                })
        tables.append({
            "name": table_name,
            "columns": table_columns,
            "primary_key": primary_key,
            "foreign_keys": table_foreign_keys,
            "unique_constraints": [primary_key] if primary_key else [],
        })
    if not db_id or not tables:
        raise ValueError("Spider tables.json entry requires db_id and at least one table")
    return {
        "source": "spider_tables_json",
        "db_id": db_id,
        "tables": tables,
    }


def load_spider_catalog(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Spider tables.json must contain a JSON list")
    catalog: dict[str, dict[str, Any]] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        normalized = normalize_spider_schema(entry)
        db_id = normalized["db_id"].lower()
        if db_id in catalog:
            raise ValueError(f"duplicate Spider db_id in tables.json: {normalized['db_id']}")
        catalog[db_id] = normalized
    if not catalog:
        raise ValueError("Spider tables.json contained no usable schemas")
    return catalog


def compact_schema(catalog_entry: dict[str, Any]) -> str:
    """Render a valid compact fallback while retaining full keys separately.

    The compact grammar supports column-level constraints only.  A composite
    primary key therefore stays exclusively in ``schema_catalog``; rendering
    every member as ``PRIMARY KEY`` would create invalid SQLite DDL and would
    incorrectly strengthen each column into a separate unique key.
    """
    parts: list[str] = []
    for table in catalog_entry.get("tables") or ():
        primary_key = [str(item) for item in table.get("primary_key") or ()]
        if not primary_key:
            primary_key = [
                str(column.get("name"))
                for column in table.get("columns") or ()
                if column.get("is_primary_key")
            ]
        unary_primary = primary_key[0] if len(primary_key) == 1 else None
        columns: list[str] = []
        for column in table.get("columns") or ():
            definition = f"{_identifier(column.get('name'))} {column.get('data_type') or 'TEXT'}"
            if unary_primary is not None and str(column.get("name")) == unary_primary:
                definition += " PRIMARY KEY"
            columns.append(definition)
        if columns:
            parts.append(f"{_identifier(table.get('name'))}({', '.join(columns)})")
    if not parts:
        raise ValueError("Spider schema has no renderable columns")
    return "; ".join(parts) + ";"


__all__ = ["compact_schema", "load_spider_catalog", "normalize_spider_schema"]
