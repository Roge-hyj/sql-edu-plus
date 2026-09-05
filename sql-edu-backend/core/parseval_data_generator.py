"""Stable public facade for the modular SQLite Phase 1 implementation."""

from core.phase1_engine import (
    generate_and_compare,
    generate_test_database,
    generate_witness_suite,
)
from core.phase1_evidence import _normalize_cell, extract_ast_diffs, transpile_to_sqlite
from core.phase1_foundation import SandboxRun
from core.phase1_sql_semantics import (
    _sqlite_declared_affinity,
    parse_schema_column_types,
    parse_schema_text,
)

__all__ = [
    "SandboxRun",
    "extract_ast_diffs",
    "generate_and_compare",
    "generate_test_database",
    "generate_witness_suite",
    "parse_schema_column_types",
    "parse_schema_text",
    "transpile_to_sqlite",
]
