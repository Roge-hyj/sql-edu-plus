"""Shared schema normalization for external SQL corpus records."""

from __future__ import annotations

import re
from typing import Any

from sqlglot import exp

from run_data_generation_boundary_tests import infer_schema
from core.parseval_data_generator import parse_schema_text


def normalize_generic_schema(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize scraped schemas and remove synthetic multi-table ambiguity."""
    extraction = str(item.get("extraction_method") or "")
    source_kind = str(item.get("source_kind") or "")
    if extraction != "generic_recursive" and source_kind != "local_external_seed":
        return item
    sql = str(item.get("sql") or "").strip()
    if not sql:
        return item
    inferred = infer_schema(sql)
    if not inferred:
        return item
    try:
        root = exp.parse_one(sql, read="mysql")
    except Exception:
        return item
    original = parse_schema_text(str(item.get("schema") or ""))
    inferred_tables = parse_schema_text(inferred)
    tables = inferred_tables or original
    if not tables:
        return item
    aliases: dict[str, str] = {}
    for node in root.find_all(exp.Table):
        name = str(node.name or "").lower()
        alias = str(node.alias or "").lower()
        if name and alias:
            aliases[alias] = name
    # Merge original columns that are genuine SQL identifiers, while dropping
    # scraper tokens such as URLs, function names and derived aliases.
    sql_tokens = {x.lower() for x in re.findall(r"\b[A-Za-z_]\w*\b", sql)}
    referenced = {str(n.name or "").lower() for n in root.find_all(exp.Column)}
    noise = {"asc", "desc", "day", "month", "year", "decimal"} | set(aliases)
    merged: dict[str, list[str]] = {}
    for table, columns in tables.items():
        values = list(columns)
        for col in original.get(table, []):
            low = col.lower()
            if low in sql_tokens and low in referenced and low not in noise and col not in values:
                values.append(col)
        merged[table] = values
    # Choose one owner for unqualified duplicate columns. Explicitly qualified
    # references are retained on each referenced table.
    qualified: dict[str, set[str]] = {}
    for node in root.find_all(exp.Column):
        col = str(node.name or "").lower()
        relation = str(node.table or "").lower()
        if col and relation:
            qualified.setdefault(col, set()).add(aliases.get(relation, relation))
    owners: dict[str, str] = {}
    for table, columns in merged.items():
        for col in columns:
            owners.setdefault(col.lower(), table)
    for col, relations in qualified.items():
        matches = [table for table in merged if table.lower() in relations]
        if matches:
            owners[col] = matches[0]
    normalized: dict[str, list[str]] = {}
    for table, columns in merged.items():
        normalized[table] = [
            col for col in columns
            if len([t for t, cs in merged.items() if col.lower() in {x.lower() for x in cs}]) == 1
            or owners.get(col.lower()) == table
            or table.lower() in qualified.get(col.lower(), set())
        ]
    schema = "; ".join(f"{table}({', '.join(cols)})" for table, cols in normalized.items()) + ";"
    return {**item, "schema": schema, "schema_normalization": "shared_v2", "source_schema": item.get("schema")}
