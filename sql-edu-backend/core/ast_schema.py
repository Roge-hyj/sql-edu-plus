"""Shared, serializable AST-difference contract for SQLite Phase 1."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp


def _node_sql(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    try:
        return node.sql(dialect="sqlite", normalize=True)
    except Exception:
        return str(node)


@dataclass
class ASTDiffNode:
    """One atomic structural difference between two SQLite queries."""

    clause_category: str
    diff_type: str
    target_table: str | None = None
    target_column: str | None = None
    standard_node: Any | None = None
    student_node: Any | None = None
    knowledge_point_id: str = "select-basic"
    severity: float = 0.5
    extra: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        if key == "clause":
            return self.clause_category
        if key == "diff_type":
            return self.diff_type
        if key == "column":
            return self.target_column
        if key == "table":
            return self.target_table
        if hasattr(self, key):
            return getattr(self, key)
        return self.extra.get(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            value = self[key]
            return value if value is not None else default
        except Exception:
            return default

    def __contains__(self, key: str) -> bool:
        if key in {"clause", "diff_type", "column", "table"}:
            return True
        if hasattr(self, key):
            return getattr(self, key) is not None
        return key in self.extra

    def matches_clause(self, *clauses: str) -> bool:
        normalized = {str(item).upper() for item in clauses if item}
        return bool(normalized) and str(self.clause_category or "").upper() in normalized

    def matches_diff_type(self, *diff_types: str) -> bool:
        normalized = {str(item) for item in diff_types if item}
        return bool(normalized) and str(self.diff_type or "") in normalized

    def to_dict(self) -> dict[str, Any]:
        safe_extra = {
            key: _node_sql(value) if isinstance(value, exp.Expression) else value
            for key, value in (self.extra or {}).items()
        }
        standard_sql = safe_extra.get("standard_sql") or _node_sql(self.standard_node)
        student_sql = safe_extra.get("student_sql") or _node_sql(self.student_node)
        return {
            "clause": self.clause_category,
            "diff_type": self.diff_type,
            "table": self.target_table,
            "column": self.target_column,
            "knowledge_point_id": self.knowledge_point_id,
            "severity": self.severity,
            "standard_sql": standard_sql,
            "student_sql": student_sql,
            "extra": safe_extra,
        }
