"""Phase 2 deterministic SQL diagnosis.

This module consumes the rich, bounded Phase 1 contract.  It never executes
SQL and never decides equivalence independently.  Its public output is a
learner-safe explanation package; reference SQL, reference AST fragments,
mutation SQL and the complete witness database stay internal.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import inspect
from itertools import islice
import json
import math
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.phase2_schema_catalog import Phase2SchemaCatalog, parse_schema_catalog
from core.scoped_query_graph import ScopedQueryGraph, build_scoped_query_graph


PUBLIC_SCHEMA_VERSION = "phase2.public.v1"
INTERNAL_SCHEMA_VERSION = "phase2.internal.v1"
DIAGNOSIS_VERSION = "phase2-mvp-2026-08"
RULE_CATALOG_VERSION = "phase2.rules.mvp20.v1"

MAX_ORDERED_DIFFS = 128
MAX_WITNESS_CASES = 2
MAX_WITNESS_ROWS = 6
MAX_WITNESS_CELLS = 8
MAX_PUBLIC_STRING = 240
MAX_FORBIDDEN_VALUES = 512
MAX_FORBIDDEN_VALUE_CHARS = 32768
_PUBLIC_IDENTIFIER = re.compile(r"^(?:[^\W\d]|_)[\w$]*$", re.UNICODE)


# This order is an internal relational-flow model.  S1..S6 below remain the
# learner-facing compatibility view, not a claim that all SQL is one flat pipe.
LOGICAL_STAGE_ORDER: tuple[str, ...] = (
    "PRECHECK",
    "CTE_PRODUCER",
    "SOURCE_JOIN",
    "ROW_FILTER",
    "GROUP_AGG",
    "GROUP_FILTER",
    "WINDOW",
    "PROJECTION",
    "DISTINCT",
    "SET_OP",
    "ROOT_ORDER",
    "PAGINATION",
    "EXTENSION",
)
_STAGE_RANK = {name: index for index, name in enumerate(LOGICAL_STAGE_ORDER)}

_EVIDENCE_RANK = {
    "AST_ONLY": 0,
    "OUTPUT_ONLY": 1,
    "PAIR_DISTINGUISHED": 2,
    "REPAIR_VERIFIED": 3,
    "CAUSAL_VERIFIED": 4,
}


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    teaching_stage: str
    title_zh: str
    title_en: str
    minimum_evidence: str


# The user's original list contains 4+4+3+3+4+2 = 20 rules.  This is an MVP
# catalogue, deliberately not advertised as complete SQL error coverage.
RULE_CATALOG: tuple[RuleSpec, ...] = (
    RuleSpec("S1_MISSING_BRIDGE", "S1", "关联路径断裂", "Missing bridge table", "join/source diff + causal witness or authoritative relationship"),
    RuleSpec("S1_CARTESIAN_PRODUCT", "S1", "笛卡尔积失控", "Cartesian product fallacy", "missing join constraint + multiplicity witness"),
    RuleSpec("S1_OUTER_JOIN_MISUSE", "S1", "外连接语义偏差", "Outer join misuse", "join type diff + dangling-row witness"),
    RuleSpec("S1_SUBQUERY_CARDINALITY", "S1", "子查询输入类型错配", "Subquery cardinality mismatch", "scalar/set shape + multi-row or execution evidence"),
    RuleSpec("S2_BOUNDARY", "S2", "临界值边界偏差", "Boundary mismatch", "comparison diff + boundary witness"),
    RuleSpec("S2_BOOLEAN_LOGIC", "S2", "复合布尔逻辑错误", "Boolean logic mismatch", "typed boolean-tree diff + witness"),
    RuleSpec("S2_NULL_LOGIC", "S2", "空值与三值逻辑陷阱", "NULL logic hazard", "null-sensitive diff + NULL witness"),
    RuleSpec("S2_AGGREGATE_IN_WHERE", "S2", "聚合过滤前置", "Aggregate used in row filter", "aggregate placement diff or execution evidence"),
    RuleSpec("S3_GRAIN_ENTITY_MISMATCH", "S3", "分组维度与实体错位", "Grouping grain/entity mismatch", "group diff + intent/schema/cardinality evidence"),
    RuleSpec("S3_GROUP_KEY_MISSING", "S3", "非聚合维度声明缺失", "Missing grouping attribute", "coarse-grain diff + result evidence"),
    RuleSpec("S3_GROUP_KEY_REDUNDANT", "S3", "分组维度冗余", "Redundant grouping attribute", "fine-grain diff + split witness"),
    RuleSpec("S4_HAVING_MISSING", "S4", "组级统计约束缺失", "Missing HAVING condition", "HAVING diff + group witness"),
    RuleSpec("S4_AGG_BOUNDARY", "S4", "聚合统计边界偏差", "Aggregate boundary mismatch", "aggregate comparison diff + group-size witness"),
    RuleSpec("S4_ROW_FILTER_IN_HAVING", "S4", "行过滤后置", "Row filter placed in HAVING", "bound WHERE/HAVING relocation pair"),
    RuleSpec("S5_FANOUT_AGGREGATE", "S5", "关联导致的度量虚高", "Fan-out aggregate inflation", "join multiplicity + aggregate delta"),
    RuleSpec("S5_COUNT_NULL_SENSITIVITY", "S5", "COUNT 空值敏感度错用", "COUNT null-sensitivity mismatch", "COUNT shape + nullable schema + NULL witness"),
    RuleSpec("S5_CASE_INCOMPLETE", "S5", "条件分支覆盖不全", "Incomplete CASE coverage", "CASE branch diff + uncovered-row witness"),
    RuleSpec("S5_TOP_LEVEL_DEDUP", "S5", "结果集去重遗漏", "Missing top-level deduplication", "DISTINCT diff + duplicate-output witness"),
    RuleSpec("S6_TOPN_WITHOUT_ORDER", "S6", "未决排序导致截断随机", "Top-N without deterministic order", "row limit + absent/incomplete order"),
    RuleSpec("S6_ORDER_OFFSET", "S6", "排序方向或偏移偏差", "Order/offset mismatch", "order/offset diff + cutoff witness"),
)
_RULE_BY_ID = {item.rule_id: item for item in RULE_CATALOG}


@dataclass(frozen=True)
class EvidenceDiff:
    diff_id: str
    obligation_id: str | None
    scope_id: str
    scope_kind: str
    logical_stage: str
    teaching_stage: str
    clause: str
    diff_type: str
    knowledge_point_id: str
    severity: float
    evidence_grade: str
    mutation_test_ids: tuple[str, ...] = ()
    scope_inferred: bool = False
    # Kept only in the internal object.  Public serialization never emits it.
    internal: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "diff_id": self.diff_id,
            "obligation_id": self.obligation_id,
            "scope_id": self.scope_id,
            "scope_kind": self.scope_kind,
            "logical_stage": self.logical_stage,
            "teaching_stage": self.teaching_stage,
            "clause": self.clause,
            "diff_type": self.diff_type,
            "knowledge_point_id": self.knowledge_point_id,
            "evidence_grade": self.evidence_grade,
        }


@dataclass(frozen=True)
class DiagnosticCandidate:
    candidate_id: str
    rule_id: str
    scope_id: str
    logical_stage: str
    teaching_stage: str
    diff_ids: tuple[str, ...]
    obligation_ids: tuple[str, ...]
    mutation_test_ids: tuple[str, ...]
    knowledge_points: tuple[str, ...]
    evidence_grade: str
    confidence: float
    severity: float
    blocking: bool
    boundary_reason: str | None = None
    # ``evidence_grade`` is intentionally the maximum grade in this candidate
    # bundle.  These partitions make that scope explicit: a UI may display the
    # whole knowledge-point union, but only ``verified_diff_ids`` support the
    # strong grade and downstream Phase 3 must use the rule_id mapping instead
    # of treating every displayed point as proven.
    verified_diff_ids: tuple[str, ...] = ()
    unverified_diff_ids: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        spec = _RULE_BY_ID.get(self.rule_id)
        return {
            "candidate_id": self.candidate_id,
            "rule_id": self.rule_id,
            "title": spec.title_zh if spec else "尚未分类的受支持差异",
            "stage": self.teaching_stage,
            "logical_stage": self.logical_stage,
            "scope_id": self.scope_id,
            "knowledge_points": list(self.knowledge_points),
            "evidence_grade": self.evidence_grade,
            "evidence_refs": {
                "diff_ids": list(self.diff_ids),
                "verified_diff_ids": list(self.verified_diff_ids),
                "unverified_diff_ids": list(self.unverified_diff_ids),
                "obligation_ids": list(self.obligation_ids),
                "mutation_test_ids": list(self.mutation_test_ids),
            },
            "knowledge_points_scope": "DISPLAY_UNION_ONLY",
        }

    def internal_dict(self) -> dict[str, Any]:
        """Bounded audit form; still contains references rather than raw SQL."""
        return {
            **self.public_dict(),
            "confidence": self.confidence,
            "severity": self.severity,
            "blocking": self.blocking,
            "boundary_reason": self.boundary_reason,
        }


@dataclass
class DiagnosticPackage:
    verdict: str
    diagnosis_status: str
    phase1_status: str
    equivalence_conclusion: str
    judge_status: str
    ordered_pipeline: list[EvidenceDiff] = field(default_factory=list)
    primary: DiagnosticCandidate | None = None
    secondary: list[DiagnosticCandidate] = field(default_factory=list)
    suppressed: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[DiagnosticCandidate] = field(default_factory=list)
    witness: dict[str, Any] | None = None
    qss: dict[str, Any] = field(default_factory=dict)
    narrative: dict[str, str] = field(default_factory=dict)
    boundary_notes: list[str] = field(default_factory=list)
    causal_edges: list[dict[str, str]] = field(default_factory=list, repr=False)
    candidate_trace: list[DiagnosticCandidate] = field(default_factory=list, repr=False)
    scoped_query_graph: dict[str, Any] = field(default_factory=dict, repr=False)
    schema_catalog: dict[str, Any] = field(default_factory=dict, repr=False)
    _forbidden_values: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": PUBLIC_SCHEMA_VERSION,
            "diagnosis_version": DIAGNOSIS_VERSION,
            "rule_catalog_version": RULE_CATALOG_VERSION,
            "verdict": self.verdict,
            "diagnosis_status": self.diagnosis_status,
            "phase1": {
                "status": self.phase1_status,
                "equivalence_conclusion": self.equivalence_conclusion,
                "judge_status": self.judge_status,
            },
            "ordered_diff_pipeline": [item.public_dict() for item in self.ordered_pipeline],
            "primary": self.primary.public_dict() if self.primary else None,
            "secondary": [item.public_dict() for item in self.secondary],
            "secondary_count": len(self.secondary),
            "suppressed_symptoms": list(self.suppressed),
            "unresolved_count": len(self.unresolved),
            "witness": self.witness,
            "qss": self.qss,
            "narrative": dict(self.narrative),
            "boundary_notes": list(dict.fromkeys(self.boundary_notes)),
        }
        return sanitize_public_package(payload, forbidden_values=self._forbidden_values)

    def to_internal_dict(self) -> dict[str, Any]:
        """Return an auditable, still bounded internal trace (never student-facing)."""
        return {
            "schema_version": INTERNAL_SCHEMA_VERSION,
            "diagnosis_version": DIAGNOSIS_VERSION,
            "rule_catalog_version": RULE_CATALOG_VERSION,
            "public": self.to_dict(),
            "scoped_query_graph": dict(self.scoped_query_graph),
            "schema_catalog": dict(self.schema_catalog),
            "candidates": [item.internal_dict() for item in self.candidate_trace],
            "causal_dag": {
                "primary_fdp_candidate_id": (
                    self.primary.candidate_id if self.primary is not None else None
                ),
                "secondary_root_candidate_ids": [
                    item.candidate_id for item in self.secondary
                ],
                "suppressed_candidate_ids": [
                    str(item.get("candidate_id") or "") for item in self.suppressed
                ],
                "unresolved_candidate_ids": [
                    item.candidate_id for item in self.unresolved
                ],
                "edges": list(self.causal_edges),
            },
            "sanitizer_report": {
                "status": "PASSED",
                "public_schema_version": PUBLIC_SCHEMA_VERSION,
                "forbidden_reference_value_count": len(self._forbidden_values),
                "raw_sql_copied": False,
                "full_witness_world_copied": False,
            },
        }


@dataclass
class _Phase1Evidence:
    executed: bool
    phase1_status: str
    equivalence_conclusion: str
    judge_status: str
    verdict: str
    data: Mapping[str, Any]
    mutations: Mapping[str, Any]
    database: Mapping[str, Sequence[Mapping[str, Any]]]
    diffs: list[EvidenceDiff]
    dependency_links: list[dict[str, str]]
    limitations: list[str]
    standard_sql: str
    student_sql: str


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _bounded_float(value: Any, default: float = 0.5) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return max(0.0, min(1.0, result))


def _stable_id(prefix: str, payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return f"{prefix}_{sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _rich_verdict(sandbox_run: Any) -> tuple[str, str, str, str]:
    data = _mapping(_field(sandbox_run, "data_evidence", {}))
    status = _upper(_field(sandbox_run, "status") or data.get("status")) or "UNKNOWN"
    conclusion = _upper(
        _field(sandbox_run, "equivalence_conclusion")
        or data.get("equivalence_conclusion")
    ) or "UNDECIDED"
    judge = _upper(_field(sandbox_run, "judge_status") or data.get("judge_status")) or "UNDECIDED"
    executed = _field(sandbox_run, "executed", False) is True
    boundary = _mapping(_field(sandbox_run, "boundary_evidence", {})) or _mapping(data.get("boundary_evidence"))
    guard = _mapping(data.get("verdict_guard"))
    supported = status in {"SUPPORTED", "SUPPORTED_WITH_LIMITS"}

    if conclusion == "NOT_EQUIVALENT" and supported and judge == "WRONG":
        return "INCORRECT", status, conclusion, judge
    if (
        conclusion == "NO_COUNTEREXAMPLE_FOUND"
        and executed
        and supported
        and judge in {"CORRECT", "OPERATIONALLY_ACCEPTED"}
        and not boundary
        and not guard
    ):
        return "CORRECT", status, conclusion, judge
    return "UNDECIDED", status, conclusion, "UNDECIDED"


def _normalize_scope(raw: Any) -> tuple[str, str, bool]:
    text = str(raw or "").strip().lower()
    inferred = not bool(text)
    if not text or text in {"root", "main", "outer"}:
        return "root", "ROOT", inferred
    text = re.sub(r"^mutation[:/]", "", text)
    if text.startswith("nested"):
        suffix = text.split(":", 1)[1] if ":" in text else text
        return f"subquery:{suffix}", "SUBQUERY", inferred
    if text.startswith("subquery"):
        return text, "SUBQUERY", inferred
    if text.startswith("cte"):
        return text, "CTE", inferred
    if text.startswith(("set", "union", "intersect", "except", "branch")):
        return text, "SET_BRANCH", inferred
    if text.startswith("derived"):
        return text, "DERIVED", inferred
    return text, "EXTENSION", inferred


def _logical_stage(clause: str, diff_type: str, mutation_clause: str = "") -> tuple[str, str]:
    clause_key = _upper(mutation_clause or clause)
    kind = str(diff_type or "").lower()
    if "cte" in clause_key or "cte" in kind or "RECURSIVE" in clause_key:
        return "CTE_PRODUCER", "S1"
    if kind == "null_sensitive_antijoin_equivalence" and clause_key == "WHERE":
        return "ROW_FILTER", "S2"
    if clause_key in {"FROM", "JOIN", "JOIN ON", "ON", "SUBQUERY"} or any(
        token in kind for token in ("join_", "from_source", "subquery_", "correlated_")
    ):
        return "SOURCE_JOIN", "S1"
    if kind == "aggregate_condition_in_where":
        return "ROW_FILTER", "S2"
    if clause_key == "HAVING" or "having" in kind:
        return "GROUP_FILTER", "S4"
    if clause_key == "CASE":
        return "PROJECTION", "S5"
    if clause_key == "WHERE" or kind in {
        "where_changed", "null_equality_changed",
        "logical_operator_changed", "logical_precedence_tree_changed",
    }:
        return "ROW_FILTER", "S2"
    if clause_key in {"GROUP", "GROUP BY"} or "group" in kind:
        return "GROUP_AGG", "S3"
    if clause_key == "WINDOW" or "window" in kind:
        return "WINDOW", "S5"
    if clause_key == "DISTINCT" or kind == "distinct_changed":
        return "DISTINCT", "S5"
    if clause_key in {"UNION", "INTERSECT", "EXCEPT", "SET"} or kind.startswith("set_"):
        return "SET_OP", "EXTENSION"
    if clause_key in {"ORDER", "ORDER BY"} or kind.startswith("order_"):
        return "ROOT_ORDER", "S6"
    if clause_key in {"LIMIT", "OFFSET"} or kind in {"limit_changed", "offset_changed"}:
        return "PAGINATION", "S6"
    if clause_key in {"SELECT", "PROJECTION", "CASE", "AGGREGATION"} or any(
        token in kind for token in ("projection", "column_", "star_", "alias_", "case_", "aggregate_", "function_argument")
    ):
        return "PROJECTION", "S5"
    return "EXTENSION", "EXTENSION"


def _mutation_index(mutations: Mapping[str, Any]) -> tuple[dict[str, list[tuple[str, Mapping[str, Any]]]], list[Mapping[str, Any]]]:
    by_diff: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    tests: list[Mapping[str, Any]] = []
    for index, raw in enumerate(_sequence(mutations.get("tests"))):
        test = _mapping(raw)
        if not test:
            continue
        tests.append(test)
        test_id = str(test.get("mutation_test_id") or test.get("test_id") or "")
        if not test_id:
            test_id = _stable_id(
                "mutation",
                {
                    "diff_ids": sorted(str(item) for item in _sequence(test.get("diff_ids"))),
                    "clause": test.get("clause"),
                    "scope": test.get("query_scope"),
                    "index": index,
                },
            )
        for diff_id in _sequence(test.get("diff_ids")):
            by_diff.setdefault(str(diff_id), []).append((test_id, test))
    return by_diff, tests


def _effectiveness_index(data: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    for raw in _sequence(data.get("obligation_effectiveness")):
        item = _mapping(raw)
        diff_id = str(item.get("diff_id") or "")
        if diff_id:
            result.setdefault(diff_id, []).append(item)
    return result


def _evidence_grade(
    *,
    effects: Sequence[Mapping[str, Any]],
    mutations: Sequence[tuple[str, Mapping[str, Any]]],
    pair_distinguished: bool,
) -> str:
    if any(item.get("causal_attribution_verified") is True for item in effects):
        return "CAUSAL_VERIFIED"

    def is_atomic_repair(item: Mapping[str, Any]) -> bool:
        """Accept a replacement as atomic proof only with an exact binding.

        A whole-clause replacement can repair the query while changing several
        dependent nodes.  That is useful bundle evidence, but it cannot prove
        that every linked diff is independently causal.
        """
        diff_ids = tuple(str(value) for value in _sequence(item.get("diff_ids")) if value)
        mutation_scope = tuple(
            str(value) for value in _sequence(item.get("mutation_scope")) if value
        )
        dependent_changes = tuple(
            str(value) for value in _sequence(item.get("dependent_changes")) if value
        )
        return (
            item.get("fixed_by_replacement") is True
            and item.get("replacement_exec_ok") is True
            and str(item.get("binding_quality") or "").lower() == "exact"
            and len(diff_ids) == 1
            and len(mutation_scope) <= 1
            and not dependent_changes
        )

    if any(
        is_atomic_repair(item)
        for _, item in mutations
    ):
        return "REPAIR_VERIFIED"
    if any(
        item.get("distinguished") is True
        and item.get("constraints_satisfied") is not False
        for item in effects
    ):
        return "PAIR_DISTINGUISHED"
    return "OUTPUT_ONLY" if pair_distinguished else "AST_ONLY"


def _ast_diff_dicts(sandbox_run: Any) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for raw in _sequence(_field(sandbox_run, "ast_diffs", ())):
        try:
            item = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
        except Exception:
            continue
        if isinstance(item, Mapping):
            result.append(item)
    return result


def _match_ast_metadata(raw: Mapping[str, Any], ast_items: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    raw_type = str(raw.get("diff_type") or "")
    raw_clause = _upper(raw.get("clause"))
    candidates = [item for item in ast_items if str(item.get("diff_type") or "") == raw_type]
    exact = [item for item in candidates if _upper(item.get("clause")) == raw_clause]
    if exact:
        candidates = exact
    raw_student = str(raw.get("student_sql") or "")
    if raw_student:
        same = [item for item in candidates if str(item.get("student_sql") or "") == raw_student]
        if same:
            candidates = same
    return candidates[0] if candidates else {}


def _parse_query_shape(sql: str) -> Any | None:
    """Parse one bounded query for structural classification, never execution."""
    if not sql or len(sql) > 32768:
        return None
    try:
        from sqlglot import parse_one

        return parse_one(sql)
    except Exception:
        return None


def _root_select(ast: Any) -> Any | None:
    try:
        from sqlglot import exp
    except Exception:
        return None
    if isinstance(ast, exp.Select):
        return ast
    if isinstance(ast, exp.Subquery) and isinstance(ast.this, exp.Select):
        return ast.this
    return None


def _query_has_direct_join(sql: str) -> bool:
    select = _root_select(_parse_query_shape(sql))
    return bool(select is not None and select.args.get("joins"))


def _query_has_result_limit(sql: str) -> bool:
    ast = _parse_query_shape(sql)
    if ast is None:
        return False
    return bool(ast.args.get("limit") or ast.args.get("fetch"))


def _query_direct_tables(sql: str) -> tuple[str, ...]:
    ast = _parse_query_shape(sql)
    select = _root_select(ast)
    if select is None:
        return ()
    try:
        from sqlglot import exp
    except Exception:
        return ()
    names: set[str] = set()
    for table in select.find_all(exp.Table):
        if table.find_ancestor(exp.Select) is select and table.name:
            names.add(str(table.name))
    return tuple(sorted(names, key=str.casefold))


def _condition_node(sql: str) -> Any | None:
    text = str(sql or "").strip()
    text = re.sub(r"^(?:WHERE|HAVING)\b", "", text, count=1, flags=re.IGNORECASE).strip()
    if not text or len(text) > 8192:
        return None
    ast = _parse_query_shape(f"SELECT 1 WHERE {text}")
    select = _root_select(ast)
    where = select.args.get("where") if select is not None else None
    return getattr(where, "this", None)


def _node_signature(node: Any) -> Any:
    try:
        from sqlglot import exp
    except Exception:
        return None
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.And, exp.Or)):
        operator = "AND" if isinstance(node, exp.And) else "OR"
        children = (_node_signature(node.left), _node_signature(node.right))
        return (operator, *sorted(children, key=repr))
    try:
        return re.sub(r"\s+", " ", node.sql(normalize=True)).strip().casefold()
    except Exception:
        return None


def _condition_signature(sql: str) -> Any:
    return _node_signature(_condition_node(sql))


def _condition_has_aggregate(sql: str) -> bool:
    node = _condition_node(sql)
    if node is None:
        return False
    try:
        from sqlglot import exp
    except Exception:
        return False
    return next(node.find_all(exp.AggFunc), None) is not None


def _query_fragment_context(query_sql: str, fragment_sql: str) -> str | None:
    """Locate one predicate in CASE/WHERE/HAVING without textual guessing."""
    ast = _parse_query_shape(query_sql)
    target = _condition_signature(fragment_sql)
    if ast is None or target is None:
        return None
    try:
        from sqlglot import exp
    except Exception:
        return None
    matches: set[str] = set()
    for owner_type, label in (
        (exp.Case, "CASE"),
        (exp.Having, "HAVING"),
        (exp.Where, "WHERE"),
    ):
        for owner in ast.find_all(owner_type):
            if any(_node_signature(node) == target for node in owner.walk()):
                matches.add(label)
                break
    # CASE is the most specific owner when a CASE expression itself appears
    # inside WHERE/HAVING.  Otherwise ambiguous placement remains unknown.
    if "CASE" in matches:
        return "CASE"
    return next(iter(matches)) if len(matches) == 1 else None


def _context_clause(
    raw: Mapping[str, Any],
    rich_diffs: Sequence[Mapping[str, Any]],
    mutation_links: Sequence[tuple[str, Mapping[str, Any]]],
) -> str:
    for _, test in mutation_links:
        clause = _upper(test.get("clause"))
        if clause:
            return clause
    clause = _upper(raw.get("predicate_clause") or raw.get("clause"))
    if clause not in {"PREDICATE", "EXPRESSION", "CONDITION", ""}:
        return clause
    standard_sql = str(raw.get("standard_sql") or "")
    student_sql = str(raw.get("student_sql") or "")
    standard_query = _upper(raw.get("standard_query_sql"))
    student_query = _upper(raw.get("student_query_sql"))
    query_text = f"{standard_query} {student_query}"
    predicate_upper = _upper(standard_sql or student_sql)
    structural_contexts = {
        context
        for context in (
            _query_fragment_context(
                str(raw.get("standard_query_sql") or ""),
                standard_sql,
            ),
            _query_fragment_context(
                str(raw.get("student_query_sql") or ""),
                student_sql,
            ),
        )
        if context
    }
    if "CASE" in structural_contexts:
        return "CASE"
    if len(structural_contexts) == 1:
        return next(iter(structural_contexts))
    if "HAVING" in query_text and (
        re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", predicate_upper)
        or " WHERE " not in query_text
    ):
        return "HAVING"
    if " WHERE " in f" {query_text} ":
        return "WHERE"
    for summary in rich_diffs:
        summary_clause = _upper(summary.get("clause"))
        if summary_clause in {"", "PREDICATE", "EXPRESSION", "CONDITION"}:
            continue
        if (
            standard_sql
            and student_sql
            and standard_sql == str(summary.get("standard_sql") or "")
            and student_sql == str(summary.get("student_sql") or "")
        ):
            return summary_clause
    return clause or "EXTENSION"


def _scope_sort_key(scope_id: str, scope_kind: str) -> tuple[int, str]:
    # Producers precede consumers; root result shaping comes after nested input
    # scopes.  Unknown scopes remain deterministic and visible.
    rank = {
        "CTE": 0,
        "DERIVED": 1,
        "SUBQUERY": 2,
        "SET_BRANCH": 3,
        "ROOT": 4,
        "EXTENSION": 5,
    }.get(scope_kind, 6)
    return rank, scope_id


def _adapt_phase1(sandbox_run: Any) -> _Phase1Evidence:
    verdict, phase1_status, conclusion, judge = _rich_verdict(sandbox_run)
    data = _mapping(_field(sandbox_run, "data_evidence", {}))
    mutations = _mapping(_field(sandbox_run, "mutation_evidence", {}))
    mutation_by_diff, _ = _mutation_index(mutations)
    effects_by_diff = _effectiveness_index(data)
    ast_items = _ast_diff_dicts(sandbox_run)
    rich_diffs = [
        _mapping(item)
        for item in _sequence(data.get("ast_diffs"))
        if isinstance(item, Mapping)
    ]
    if not rich_diffs:
        rich_diffs = list(ast_items)

    pair_distinguished = bool(
        data.get("any_world_distinguished")
        or data.get("any_obligation_distinguished")
        or data.get("only_in_standard_sample")
        or data.get("only_in_student_sample")
        or verdict == "INCORRECT"
    )
    limitations: list[str] = []
    diffs: list[EvidenceDiff] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(rich_diffs[:MAX_ORDERED_DIFFS]):
        ast_meta = _match_ast_metadata(raw, ast_items)
        clause = _context_clause(raw, rich_diffs, mutation_by_diff.get(str(raw.get("diff_id") or ""), ()))
        diff_type = str(raw.get("diff_type") or ast_meta.get("diff_type") or "unknown_diff")
        diff_id = str(raw.get("diff_id") or "")
        if not diff_id:
            diff_id = _stable_id(
                "diff",
                {
                    "clause": clause,
                    "diff_type": diff_type,
                    "table": raw.get("table") or ast_meta.get("table"),
                    "column": raw.get("column") or ast_meta.get("column"),
                    "standard": raw.get("standard_sql") or ast_meta.get("standard_sql"),
                    "student": raw.get("student_sql") or ast_meta.get("student_sql"),
                },
            )
        if diff_id in seen_ids:
            continue
        seen_ids.add(diff_id)
        obligation_id = str(raw.get("obligation_id") or "") or None
        if obligation_id is None and diff_id.startswith("diff_"):
            obligation_id = "obligation_" + diff_id.removeprefix("diff_")

        links = mutation_by_diff.get(diff_id, [])
        scope_raw = raw.get("query_scope") or _mapping(raw.get("extra")).get("query_scope")
        if not scope_raw:
            scope_raw = next((test.get("query_scope") for _, test in links if test.get("query_scope")), None)
        if not scope_raw:
            scope_raw = _mapping(ast_meta.get("extra")).get("query_scope")
        scope_id, scope_kind, scope_inferred = _normalize_scope(scope_raw)
        if scope_inferred:
            limitations.append(f"{diff_id}: query_scope missing; conservatively attached to root")

        mutation_clause = next((str(test.get("clause")) for _, test in links if test.get("clause")), "")
        logical_stage, teaching_stage = _logical_stage(clause, diff_type, mutation_clause)
        effects = effects_by_diff.get(diff_id, [])
        grade = _evidence_grade(
            effects=effects,
            mutations=links,
            pair_distinguished=pair_distinguished,
        )
        knowledge_point = str(
            raw.get("knowledge_point_id")
            or ast_meta.get("knowledge_point_id")
            or "unclassified"
        )
        severity = _bounded_float(raw.get("severity", ast_meta.get("severity", 0.5)))
        internal = {
            "raw": dict(raw),
            "effects": [dict(item) for item in effects],
            "mutations": [dict(item) for _, item in links],
        }
        diffs.append(
            EvidenceDiff(
                diff_id=diff_id,
                obligation_id=obligation_id,
                scope_id=scope_id,
                scope_kind=scope_kind,
                logical_stage=logical_stage,
                teaching_stage=teaching_stage,
                clause=clause,
                diff_type=diff_type,
                knowledge_point_id=knowledge_point,
                severity=severity,
                evidence_grade=grade,
                mutation_test_ids=tuple(sorted(test_id for test_id, _ in links)),
                scope_inferred=scope_inferred,
                internal=internal,
            )
        )

    diffs.sort(
        key=lambda item: (
            *_scope_sort_key(item.scope_id, item.scope_kind),
            _STAGE_RANK.get(item.logical_stage, 999),
            item.diff_type,
            item.diff_id,
        )
    )
    known_diff_ids = {item.diff_id for item in diffs}
    dependency_links: list[dict[str, str]] = []
    allowed_dependency_types = {
        "CAUSES",
        "SUPPORTS",
        "RELOCATES_TO",
        "AMPLIFIES",
        "MASKS",
        "CO_OCCURS",
    }
    for raw_link in _sequence(data.get("diagnostic_dependencies"))[:256]:
        link = _mapping(raw_link)
        source = str(link.get("from_diff_id") or "")
        target = str(link.get("to_diff_id") or "")
        edge_type = _upper(link.get("type"))
        if (
            source in known_diff_ids
            and target in known_diff_ids
            and source != target
            and edge_type in allowed_dependency_types
        ):
            dependency_links.append(
                {"from_diff_id": source, "to_diff_id": target, "type": edge_type}
            )
        elif link:
            limitations.append("invalid Phase 1 diagnostic dependency was ignored")
    dependency_links = [
        {"from_diff_id": source, "to_diff_id": target, "type": edge_type}
        for source, target, edge_type in sorted(
            {
                (item["from_diff_id"], item["to_diff_id"], item["type"])
                for item in dependency_links
            }
        )
    ]
    database_raw = _mapping(_field(sandbox_run, "test_database", {}))
    database: dict[str, Sequence[Mapping[str, Any]]] = {}
    for table, rows in database_raw.items():
        database[str(table)] = tuple(
            _mapping(row) for row in _sequence(rows) if isinstance(row, Mapping)
        )
    return _Phase1Evidence(
        executed=_field(sandbox_run, "executed", False) is True,
        phase1_status=phase1_status,
        equivalence_conclusion=conclusion,
        judge_status=judge,
        verdict=verdict,
        data=data,
        mutations=mutations,
        database=database,
        diffs=diffs,
        dependency_links=dependency_links,
        limitations=list(dict.fromkeys(limitations)),
        standard_sql=str(_field(sandbox_run, "standard_sqlite", "") or ""),
        student_sql=str(_field(sandbox_run, "student_sqlite", "") or ""),
    )


def _apply_scoped_query_graph(
    evidence: _Phase1Evidence,
    graph: ScopedQueryGraph,
) -> dict[str, int]:
    """Attach diffs only to a proven, side-neutral conceptual scope.

    Phase 1 deliberately exposes separate ``standard:*`` and ``student:*``
    nodes.  Selecting either side here would merge unrelated nested queries or
    leak reference-side topology into the learner-facing pipeline.  A diff
    therefore receives a conceptual scope only when the graph proves the
    exact pair.  All other diffs remain individually unscoped.
    """

    bindings = {item.diff_id: item for item in graph.conceptual_bindings}
    nodes = {item.scope_id: item for item in graph.scopes}
    conceptual_order: dict[str, int] = {}
    for node in graph.scopes:
        conceptual = node.conceptual_scope_id
        if conceptual and conceptual not in conceptual_order:
            conceptual_order[conceptual] = len(conceptual_order)

    remapped: list[EvidenceDiff] = []
    limitations = list(evidence.limitations)
    for diff in evidence.diffs:
        conceptual = graph.conceptual_scope_for_diff(diff.diff_id)
        binding = bindings.get(diff.diff_id)
        if conceptual and binding is not None:
            kinds = {
                nodes[scope_id].scope_kind
                for scope_id in binding.scope_ids
                if scope_id in nodes and nodes[scope_id].scope_kind != "UNKNOWN"
            }
            if len(kinds) == 1:
                scope_kind = next(iter(kinds))
            else:
                scope_kind = "UNKNOWN"
                limitations.append(
                    f"{diff.diff_id}: conceptual scope kind is missing or conflicting"
                )
            remapped.append(
                replace(
                    diff,
                    scope_id=conceptual,
                    scope_kind=scope_kind,
                    scope_inferred=False,
                )
            )
            continue

        unscoped_id = f"unscoped:{diff.diff_id}"[:160]
        remapped.append(
            replace(
                diff,
                scope_id=unscoped_id,
                scope_kind="UNKNOWN",
                scope_inferred=True,
            )
        )
        limitations.append(
            f"{diff.diff_id}: exact paired conceptual scope unavailable; kept unscoped"
        )

    remapped.sort(
        key=lambda item: (
            conceptual_order.get(item.scope_id, len(conceptual_order)),
            _STAGE_RANK.get(item.logical_stage, 999),
            item.diff_type,
            item.diff_id,
        )
    )
    evidence.diffs = remapped
    evidence.limitations = list(dict.fromkeys(limitations))
    return conceptual_order


_SUMMARY_DIFF_TYPES = {
    "projection_changed",
    "where_changed",
    "group_by_changed",
    "having_changed",
    "order_by_changed",
    "limit_changed",
    "case_changed",
}


def _raw_sql_pair(diff: EvidenceDiff) -> tuple[str, str]:
    raw = _mapping(diff.internal.get("raw"))
    return str(raw.get("standard_sql") or ""), str(raw.get("student_sql") or "")


def _is_redundant_summary(diff: EvidenceDiff, all_diffs: Sequence[EvidenceDiff]) -> bool:
    if diff.diff_type not in _SUMMARY_DIFF_TYPES:
        return False
    # ``case_changed`` carries the branch-topology evidence that authorizes
    # the frozen S5_CASE_INCOMPLETE rule.  Projection/aggregate summaries may
    # share the same SQL pair, but dropping the CASE node would leave only a
    # lower-confidence function-argument symptom and make a verified CASE
    # witness impossible to project into Phase 3.  Keep this semantic summary
    # even when an implementation-level CASE diff is present.
    if diff.diff_type == "case_changed":
        return False
    std_sql, stu_sql = _raw_sql_pair(diff)
    for other in all_diffs:
        if other.diff_id == diff.diff_id or other.scope_id != diff.scope_id:
            continue
        if other.diff_type in _SUMMARY_DIFF_TYPES:
            continue
        if other.logical_stage != diff.logical_stage:
            continue
        other_std, other_stu = _raw_sql_pair(other)
        if (std_sql and stu_sql and std_sql == other_std and stu_sql == other_stu) or (
            diff.obligation_id is None and other.obligation_id is not None
        ):
            return True
    return False


def _schema_nullable_columns(catalog: Phase2SchemaCatalog) -> set[tuple[str, str]]:
    """Return only explicitly nullable columns; unknown is never treated true."""
    return {
        (table.name.casefold(), column.name.casefold())
        for table in catalog.tables
        for column in table.columns
        if column.nullable is True
    }


def _has_direct_aggregate_in_where(sql: str) -> bool:
    """Detect an aggregate owned by the same SELECT as its WHERE clause."""
    if not sql or "WHERE" not in sql.upper():
        return False
    try:
        from sqlglot import exp, parse_one

        ast = parse_one(sql)
    except Exception:
        return False
    selects = list(ast.find_all(exp.Select))
    if isinstance(ast, exp.Select):
        selects.insert(0, ast)
    seen: set[int] = set()
    for select in selects:
        if id(select) in seen:
            continue
        seen.add(id(select))
        where = select.args.get("where")
        if not isinstance(where, exp.Where):
            continue
        for aggregate in where.find_all(exp.AggFunc):
            owner = aggregate.parent
            while owner is not None and not isinstance(owner, exp.Select):
                owner = owner.parent
            if owner is select:
                return True
    return False


def _rule_for_diff(
    diff: EvidenceDiff,
    *,
    catalog: Phase2SchemaCatalog,
    fanout_signal: bool,
    missing_bridge_signal: bool,
    topn_signal: bool,
) -> str:
    kind = diff.diff_type.lower()
    stage = diff.logical_stage
    raw = _mapping(diff.internal.get("raw"))
    std_sql, stu_sql = _raw_sql_pair(diff)
    combined = f"{std_sql} {stu_sql}".upper()

    if kind == "join_missing":
        return (
            "S1_MISSING_BRIDGE"
            if missing_bridge_signal
            else "UNCLASSIFIED_SUPPORTED_DIFF"
        )
    if kind == "join_type_changed":
        def join_side(value: Any, sql: str) -> str:
            declared = _upper(value)
            if declared:
                return declared
            match = re.search(
                r"\b(LEFT|RIGHT|FULL|CROSS|INNER)\s+(?:OUTER\s+)?JOIN\b",
                _upper(sql),
            )
            if match:
                return match.group(1)
            return "INNER" if re.search(r"\bJOIN\b", _upper(sql)) else ""

        standard_side = join_side(raw.get("standard_side"), std_sql)
        student_side = join_side(raw.get("student_side"), stu_sql)
        if student_side == "CROSS" and standard_side != "CROSS":
            return "S1_CARTESIAN_PRODUCT"
        if {standard_side, student_side} & {"LEFT", "RIGHT", "FULL"}:
            return "S1_OUTER_JOIN_MISUSE"
        return "UNCLASSIFIED_SUPPORTED_DIFF"
    if kind in {
        "correlated_predicate_changed",
        "correlated_subquery_changed",
        "subquery_predicate_changed",
    }:
        # EXISTS/NOT EXISTS and correlated membership edits change the set of
        # outer rows admitted by a subquery.  They are cardinality semantics,
        # not a generic WHERE mismatch, when Phase 1 supplies the correlated
        # witness and repair evidence.
        return "S1_SUBQUERY_CARDINALITY"
    if kind == "comparison_operator_changed":
        standard_op = _upper(raw.get("standard_op"))
        student_op = _upper(raw.get("student_op"))
        has_subquery_operand = (
            "SELECT" in _upper(std_sql)
            or "SELECT" in _upper(stu_sql)
            or raw.get("standard_value_kind") == "expression"
            or raw.get("student_value_kind") == "expression"
        )
        if has_subquery_operand and (
            {standard_op, student_op} & {"IN", "ANY", "ALL"}
            and {standard_op, student_op} & {"EQ", "NEQ", "GT", "GTE", "LT", "LTE"}
        ):
            return "S1_SUBQUERY_CARDINALITY"
        # The boundary families describe an open/closed endpoint mistake,
        # not every possible comparison rewrite.  In particular ``=`` vs
        # ``!=`` and reversed inequalities express different predicates and
        # must remain a generic supported mismatch even when Phase 1 proves
        # that the atomic edit is causal.
        if frozenset({standard_op, student_op}) in {
            frozenset({"GT", "GTE"}),
            frozenset({"LT", "LTE"}),
        }:
            if stage == "GROUP_FILTER":
                return "S4_AGG_BOUNDARY"
            if stage == "ROW_FILTER":
                return "S2_BOUNDARY"
    if kind in {"logical_operator_changed", "logical_precedence_tree_changed"}:
        return "S2_BOOLEAN_LOGIC"
    if kind in {
        "null_equality_changed",
        "null_predicate_negation_changed",
        "null_sensitive_antijoin_equivalence",
    } and stage == "ROW_FILTER":
        return "S2_NULL_LOGIC"
    if (
        kind == "predicate_missing"
        and stage == "ROW_FILTER"
        and _condition_signature(std_sql) is not None
        and re.search(r"\bIS\s+(?:NOT\s+)?NULL\b", std_sql, flags=re.IGNORECASE)
    ):
        return "S2_NULL_LOGIC"
    if kind == "aggregate_condition_in_where":
        return "S2_AGGREGATE_IN_WHERE"
    if kind == "grouping_grain_too_coarse":
        return "S3_GROUP_KEY_MISSING"
    if kind == "grouping_grain_too_fine":
        return "S3_GROUP_KEY_REDUNDANT"
    if kind in {"group_by_expression_changed", "group_by_changed"}:
        return "S3_GRAIN_ENTITY_MISMATCH"
    if kind == "having_changed" and std_sql and not stu_sql:
        return "S4_HAVING_MISSING"
    if kind == "predicate_missing" and stage == "GROUP_FILTER":
        return "S4_HAVING_MISSING"
    if kind in {"having_predicate_relocated", "row_filter_in_having"}:
        return "S4_ROW_FILTER_IN_HAVING"
    if kind in {
        "case_changed",
        "case_else_missing",
        "case_else_added",
        "case_when_missing",
        "case_when_added",
    } or (
        kind == "function_argument_changed"
        and (
            str(raw.get("function") or "").upper() == "CASE"
            or "CASE" in combined
        )
    ):
        return "S5_CASE_INCOMPLETE"
    if (
        kind == "distinct_changed"
        and diff.scope_kind == "ROOT"
        and _upper(std_sql) in {"TRUE", "DISTINCT"}
    ):
        return "S5_TOP_LEVEL_DEDUP"
    if kind in {
        "aggregate_argument_changed",
        "aggregate_function_changed",
        "aggregate_distinct_changed",
    } and "COUNT" in combined:
        # COUNT(DISTINCT entity) -> COUNT(entity), together with Phase 1's
        # duplicate-row delta on a joined input, is the structural signature
        # of fan-out sensitivity rather than NULL sensitivity.  Whether it is
        # blocking is still decided solely by the atomic evidence grade.
        standard_upper = _upper(std_sql)
        student_upper = _upper(stu_sql)
        standard_distinct_count = bool(
            re.search(r"\bCOUNT\s*\(\s*DISTINCT\b", standard_upper)
        )
        student_distinct_count = bool(
            re.search(r"\bCOUNT\s*\(\s*DISTINCT\b", student_upper)
        )
        if fanout_signal and standard_distinct_count and not student_distinct_count:
            return "S5_FANOUT_AGGREGATE"
        nullable = _schema_nullable_columns(catalog)
        column = str(raw.get("column") or "").lower()
        table = str(
            raw.get("table")
            or raw.get("standard_source_table")
            or raw.get("student_source_table")
            or ""
        ).lower()
        has_nullable_fact = bool(table and column and (table, column) in nullable)
        standard_count_star = bool(
            re.search(r"\bCOUNT\s*\(\s*\*\s*\)", standard_upper)
        )
        student_count_star = bool(
            re.search(r"\bCOUNT\s*\(\s*\*\s*\)", student_upper)
        )
        if has_nullable_fact and standard_count_star != student_count_star:
            return "S5_COUNT_NULL_SENSITIVITY"
    if kind in {"order_direction_changed", "offset_changed"}:
        return "S6_ORDER_OFFSET"
    if kind == "limit_changed" and (
        _upper(std_sql).startswith("OFFSET") or _upper(stu_sql).startswith("OFFSET")
    ):
        return "S6_ORDER_OFFSET"
    if kind == "top_n_ordering_missing":
        return "S6_TOPN_WITHOUT_ORDER"
    if kind == "order_by_tiebreaker_missing" and topn_signal:
        return "S6_TOPN_WITHOUT_ORDER"

    if kind in {
        "set_operator_changed",
        "set_modifier_changed",
        "set_all_modifier_changed",
    } and "UNION" in combined:
        # UNION versus UNION ALL is a top-level duplicate-elimination error.
        # The guard on the raw SQL keeps the frozen MVP mapping from
        # mislabelling INTERSECT/EXCEPT or synthetic set nodes that lack a
        # concrete operator spelling.
        return "S5_TOP_LEVEL_DEDUP"

    # Preserve supported advanced differences without pretending they belong
    # to one of the 20 MVP families.
    if stage in {"SET_OP", "WINDOW", "CTE_PRODUCER"}:
        return "UNCLASSIFIED_SUPPORTED_DIFF"
    if (
        kind in {"where_changed", "predicate_missing", "predicate_added"}
        and stage == "ROW_FILTER"
    ):
        return "S2_ROW_FILTER_MISMATCH"
    if kind == "having_changed":
        return "S4_GROUP_FILTER_MISMATCH"
    if kind in {"order_by_changed", "limit_changed"}:
        return "S6_RESULT_SHAPING_MISMATCH"
    return "UNCLASSIFIED_SUPPORTED_DIFF"


def _candidate_from_group(
    rule_id: str,
    scope_id: str,
    diffs: Sequence[EvidenceDiff],
) -> DiagnosticCandidate:
    grades = [item.evidence_grade for item in diffs]
    grade = max(grades, key=lambda item: _EVIDENCE_RANK.get(item, -1))
    severity = max((item.severity for item in diffs), default=0.5)
    grade_score = _EVIDENCE_RANK.get(grade, 0) / max(_EVIDENCE_RANK.values())
    confidence = min(0.99, 0.42 + 0.42 * grade_score + 0.16 * severity)
    diff_ids = tuple(sorted({item.diff_id for item in diffs}))
    verified_diff_ids = tuple(
        sorted(
            {
                item.diff_id
                for item in diffs
                if item.evidence_grade in {"REPAIR_VERIFIED", "CAUSAL_VERIFIED"}
            }
        )
    )
    unverified_diff_ids = tuple(
        diff_id for diff_id in diff_ids if diff_id not in set(verified_diff_ids)
    )
    obligations = tuple(sorted({item.obligation_id for item in diffs if item.obligation_id}))
    mutation_ids = tuple(sorted({mid for item in diffs for mid in item.mutation_test_ids}))
    kps = tuple(sorted({item.knowledge_point_id for item in diffs if item.knowledge_point_id}))
    first = min(diffs, key=lambda item: (_STAGE_RANK.get(item.logical_stage, 999), item.diff_id))
    spec = _RULE_BY_ID.get(rule_id)
    teaching_stage = spec.teaching_stage if spec is not None else first.teaching_stage
    logical_stage = first.logical_stage
    if spec is not None and teaching_stage != first.teaching_stage:
        logical_stage = {
            "S1": "SOURCE_JOIN",
            "S2": "ROW_FILTER",
            "S3": "GROUP_AGG",
            "S4": "GROUP_FILTER",
            "S5": "PROJECTION",
            "S6": "ROOT_ORDER",
        }[teaching_stage]
    blocking = grade in {"REPAIR_VERIFIED", "CAUSAL_VERIFIED"}
    boundary_reason = None if blocking else "candidate lacks atomic causal or repair verification"
    candidate_id = _stable_id(
        "candidate",
        {"rule_id": rule_id, "scope_id": scope_id, "diff_ids": diff_ids},
    )
    return DiagnosticCandidate(
        candidate_id=candidate_id,
        rule_id=rule_id,
        scope_id=scope_id,
        logical_stage=logical_stage,
        teaching_stage=teaching_stage,
        diff_ids=diff_ids,
        obligation_ids=obligations,
        mutation_test_ids=mutation_ids,
        knowledge_points=kps,
        evidence_grade=grade,
        confidence=round(confidence, 4),
        severity=round(severity, 4),
        blocking=blocking,
        boundary_reason=boundary_reason,
        verified_diff_ids=verified_diff_ids,
        unverified_diff_ids=unverified_diff_ids,
    )


def _synthetic_candidate(
    rule_id: str,
    diffs: Sequence[EvidenceDiff],
    *,
    scope_id: str | None = None,
    grade: str = "OUTPUT_ONLY",
) -> DiagnosticCandidate:
    if scope_id is None:
        scopes = {item.scope_id for item in diffs}
        scope_id = (
            next(iter(scopes))
            if len(scopes) == 1
            else f"unscoped:signal:{rule_id.lower()}"
        )
    if diffs:
        candidate = _candidate_from_group(rule_id, scope_id, diffs)
        if _EVIDENCE_RANK.get(grade, 0) > _EVIDENCE_RANK.get(candidate.evidence_grade, 0):
            candidate = replace(
                candidate,
                evidence_grade=grade,
                blocking=grade in {"REPAIR_VERIFIED", "CAUSAL_VERIFIED"},
                boundary_reason=None if grade != "OUTPUT_ONLY" else candidate.boundary_reason,
            )
        return candidate
    spec = _RULE_BY_ID[rule_id]
    candidate_id = _stable_id("candidate", {"rule_id": rule_id, "scope_id": scope_id})
    return DiagnosticCandidate(
        candidate_id=candidate_id,
        rule_id=rule_id,
        scope_id=scope_id,
        logical_stage={"S1": "SOURCE_JOIN", "S5": "PROJECTION", "S6": "ROOT_ORDER"}.get(spec.teaching_stage, "EXTENSION"),
        teaching_stage=spec.teaching_stage,
        diff_ids=(),
        obligation_ids=(),
        mutation_test_ids=(),
        knowledge_points=(),
        evidence_grade=grade,
        confidence=0.55,
        severity=0.6,
        blocking=grade in {"REPAIR_VERIFIED", "CAUSAL_VERIFIED"},
        boundary_reason="signal-level candidate without an atomic diff binding",
    )


def _diffs_by_scope(
    diffs: Sequence[EvidenceDiff],
) -> dict[str, list[EvidenceDiff]]:
    grouped: dict[str, list[EvidenceDiff]] = {}
    for diff in diffs:
        grouped.setdefault(diff.scope_id, []).append(diff)
    return grouped


def _diff_query_contexts(
    evidence: _Phase1Evidence,
    diff: EvidenceDiff,
) -> tuple[str, ...]:
    raw = _mapping(diff.internal.get("raw"))
    values = [
        str(raw.get("standard_query_sql") or ""),
        str(raw.get("student_query_sql") or ""),
    ]
    # The full Phase 1 SQL pair is valid scope evidence only for a proven root
    # binding.  Nested scopes without their own query text fail closed.
    if diff.scope_kind == "ROOT":
        values.extend((evidence.standard_sql, evidence.student_sql))
    return tuple(dict.fromkeys(item for item in values if item))


def _missing_bridge_is_declared(
    evidence: _Phase1Evidence,
    diff: EvidenceDiff,
    catalog: Phase2SchemaCatalog,
) -> bool:
    raw = _mapping(diff.internal.get("raw"))
    missing = str(raw.get("table") or raw.get("target_table") or "")
    if not missing or catalog.table(missing) is None:
        return False
    student_context = str(raw.get("student_query_sql") or "")
    if not student_context and diff.scope_kind == "ROOT":
        student_context = evidence.student_sql
    tables = [
        table
        for table in _query_direct_tables(student_context)
        if table.casefold() != missing.casefold()
    ]
    if len(tables) < 2:
        return False
    declared = {
        bridge.casefold()
        for index, left in enumerate(tables)
        for right in tables[index + 1 :]
        for bridge in catalog.bridge_tables(left, right)
    }
    return missing.casefold() in declared


def _declared_fanout(
    evidence: _Phase1Evidence,
    diff: EvidenceDiff,
    catalog: Phase2SchemaCatalog,
) -> bool:
    raw = _mapping(diff.internal.get("raw"))
    contexts = _diff_query_contexts(evidence, diff)
    query_sql = next((item for item in contexts if _query_has_direct_join(item)), "")
    select = _root_select(_parse_query_shape(query_sql))
    if select is None:
        return False
    try:
        from sqlglot import exp, parse_one
    except Exception:
        return False

    aliases: dict[str, str] = {}
    for table in select.find_all(exp.Table):
        if table.find_ancestor(exp.Select) is not select or not table.name:
            continue
        physical = str(table.name)
        aliases[physical.casefold()] = physical
        if table.alias:
            aliases[str(table.alias).casefold()] = physical

    base = str(raw.get("table") or raw.get("standard_source_table") or "")
    if not base:
        fragment = str(raw.get("standard_sql") or "")
        try:
            projection = parse_one(f"SELECT {fragment}")
        except Exception:
            projection = None
        column = projection.find(exp.Column) if projection is not None else None
        if isinstance(column, exp.Column) and column.table:
            base = aliases.get(str(column.table).casefold(), "")
    base_table = catalog.table(base)
    if base_table is None:
        return False
    for joined in dict.fromkeys(aliases.values()):
        if joined.casefold() == base_table.name.casefold():
            continue
        if catalog.may_fan_out(base_table.name, joined) is True:
            return True
    return False


def _detect_candidates(
    evidence: _Phase1Evidence,
    *,
    catalog: Phase2SchemaCatalog,
) -> list[DiagnosticCandidate]:
    grouped: dict[tuple[str, str], list[EvidenceDiff]] = {}
    usable_diffs = [
        item for item in evidence.diffs if not _is_redundant_summary(item, evidence.diffs)
    ]
    duplicate_delta = int(evidence.data.get("student_duplicate_row_count") or 0) - int(
        evidence.data.get("standard_duplicate_row_count") or 0
    )
    fanout_result_signal = duplicate_delta > 0 and bool(
        re.search(r"\bJOIN\b", evidence.student_sql, flags=re.IGNORECASE)
    )
    join_diffs = [item for item in usable_diffs if item.logical_stage == "SOURCE_JOIN"]
    join_scope_ids = {item.scope_id for item in join_diffs}
    for diff in usable_diffs:
        query_contexts = _diff_query_contexts(evidence, diff)
        direct_join_context = (
            diff.diff_type == "aggregate_distinct_changed"
            and any(_query_has_direct_join(sql) for sql in query_contexts)
        )
        rule_id = _rule_for_diff(
            diff,
            catalog=catalog,
            # A result-level multiplicity delta is global.  Bind it only to a
            # root aggregate, or to a nested scope that also has an explicit
            # join/source diff.  Otherwise a JOIN in another scope could
            # falsely label this aggregate.
            fanout_signal=(
                (
                    fanout_result_signal
                    and (diff.scope_kind == "ROOT" or diff.scope_id in join_scope_ids)
                )
                or (
                    diff.diff_type == "aggregate_distinct_changed"
                    and direct_join_context
                    and _declared_fanout(evidence, diff, catalog)
                )
            ),
            missing_bridge_signal=_missing_bridge_is_declared(
                evidence,
                diff,
                catalog,
            ),
            topn_signal=(
                diff.diff_type == "order_by_tiebreaker_missing"
                and any(_query_has_result_limit(sql) for sql in query_contexts)
            ),
        )
        grouped.setdefault((diff.scope_id, rule_id), []).append(diff)
    candidates = [
        _candidate_from_group(rule_id, scope_id, items)
        for (scope_id, rule_id), items in grouped.items()
    ]

    data = evidence.data
    # Phase 1 represents a WHERE -> HAVING relocation as two clause diffs.
    # Pair them only when the exact non-aggregate condition is structurally
    # identical and both nodes belong to the same proven conceptual scope.
    # This avoids inventing the legacy ``row_filter_in_having`` diff type.
    by_scope = _diffs_by_scope(usable_diffs)
    for scope_id, scoped_diffs in by_scope.items():
        where_diffs = [item for item in scoped_diffs if item.diff_type == "where_changed"]
        having_diffs = [item for item in scoped_diffs if item.diff_type == "having_changed"]
        for where_diff in where_diffs:
            standard_where, student_where = _raw_sql_pair(where_diff)
            if not standard_where or student_where:
                continue
            standard_signature = _condition_signature(standard_where)
            if standard_signature is None or _condition_has_aggregate(standard_where):
                continue
            for having_diff in having_diffs:
                standard_having, student_having = _raw_sql_pair(having_diff)
                if standard_having or not student_having:
                    continue
                if _condition_signature(student_having) != standard_signature:
                    continue
                relocation_diffs = (where_diff, having_diff)
                if not any(
                    item.scope_id == scope_id
                    and item.rule_id == "S4_ROW_FILTER_IN_HAVING"
                    for item in candidates
                ):
                    candidates.append(
                        _synthetic_candidate(
                            "S4_ROW_FILTER_IN_HAVING",
                            relocation_diffs,
                            scope_id=scope_id,
                            grade=max(
                                (item.evidence_grade for item in relocation_diffs),
                                key=lambda value: _EVIDENCE_RANK.get(value, -1),
                            ),
                        )
                    )
                break

    if data.get("suspected_cartesian_product") is True and not any(
        item.rule_id == "S1_CARTESIAN_PRODUCT" for item in candidates
    ):
        for scope_id, scoped_diffs in _diffs_by_scope(join_diffs).items():
            candidates.append(
                _synthetic_candidate(
                    "S1_CARTESIAN_PRODUCT",
                    scoped_diffs,
                    scope_id=scope_id,
                )
            )

    aggregate_diffs = [
        item for item in usable_diffs
        if "aggregate" in item.diff_type or item.logical_stage in {"GROUP_AGG", "PROJECTION"}
    ]
    if duplicate_delta > 0 and join_diffs and aggregate_diffs and not any(
        item.rule_id == "S5_FANOUT_AGGREGATE" for item in candidates
    ):
        joins_by_scope = _diffs_by_scope(join_diffs)
        aggregates_by_scope = _diffs_by_scope(aggregate_diffs)
        for scope_id in sorted(set(joins_by_scope) & set(aggregates_by_scope)):
            scoped_diffs = [
                *joins_by_scope[scope_id],
                *aggregates_by_scope[scope_id],
            ]
            candidates.append(
                _synthetic_candidate(
                    "S5_FANOUT_AGGREGATE",
                    scoped_diffs,
                    scope_id=scope_id,
                    grade=max(
                        (item.evidence_grade for item in scoped_diffs),
                        key=lambda value: _EVIDENCE_RANK.get(value, -1),
                    ),
                )
            )

    student_sql = evidence.student_sql
    has_limit = bool(re.search(r"\bLIMIT\b", student_sql, flags=re.IGNORECASE))
    has_order = bool(re.search(r"\bORDER\s+BY\b", student_sql, flags=re.IGNORECASE))
    shaping_diffs = [
        item for item in usable_diffs if item.logical_stage in {"ROOT_ORDER", "PAGINATION"}
    ]
    if has_limit and not has_order and shaping_diffs and not any(
        item.rule_id == "S6_TOPN_WITHOUT_ORDER" for item in candidates
    ):
        for scope_id, scoped_diffs in _diffs_by_scope(shaping_diffs).items():
            candidates.append(
                _synthetic_candidate(
                    "S6_TOPN_WITHOUT_ORDER",
                    scoped_diffs,
                    scope_id=scope_id,
                )
            )

    if _has_direct_aggregate_in_where(evidence.student_sql) and not any(
        item.rule_id == "S2_AGGREGATE_IN_WHERE" for item in candidates
    ):
        placement_diffs = [
            item for item in usable_diffs
            if item.logical_stage in {"ROW_FILTER", "SOURCE_JOIN"}
            and item.diff_type in {"where_changed", "subquery_removed", "aggregate_condition_in_where"}
        ]
        by_scope = _diffs_by_scope(placement_diffs)
        if not by_scope:
            by_scope = {"unscoped:signal:aggregate-in-where": []}
        for scope_id, scoped_diffs in by_scope.items():
            candidates.append(
                _synthetic_candidate(
                    "S2_AGGREGATE_IN_WHERE",
                    scoped_diffs,
                    scope_id=scope_id,
                    grade=max(
                        (item.evidence_grade for item in scoped_diffs),
                        key=lambda value: _EVIDENCE_RANK.get(value, -1),
                        default="OUTPUT_ONLY",
                    ),
                )
            )

    # Stable de-duplication in case signal and structural paths converge.
    by_key: dict[tuple[str, str, tuple[str, ...]], DiagnosticCandidate] = {}
    for candidate in candidates:
        key = (candidate.scope_id, candidate.rule_id, candidate.diff_ids)
        existing = by_key.get(key)
        if existing is None or _EVIDENCE_RANK.get(candidate.evidence_grade, 0) > _EVIDENCE_RANK.get(existing.evidence_grade, 0):
            by_key[key] = candidate
    return sorted(by_key.values(), key=_candidate_sort_key)


def _candidate_sort_key(
    candidate: DiagnosticCandidate,
    scope_order: Mapping[str, int] | None = None,
) -> tuple[Any, ...]:
    order = scope_order or {}
    return (
        0 if candidate.blocking else 1,
        order.get(candidate.scope_id, len(order)),
        _STAGE_RANK.get(candidate.logical_stage, 999),
        -_EVIDENCE_RANK.get(candidate.evidence_grade, 0),
        -candidate.severity,
        candidate.scope_id,
        candidate.rule_id,
        candidate.candidate_id,
    )


def _bundle_and_rank(
    candidates: Sequence[DiagnosticCandidate],
    *,
    dependency_links: Sequence[Mapping[str, str]] = (),
    scope_order: Mapping[str, int] | None = None,
) -> tuple[
    DiagnosticCandidate | None,
    list[DiagnosticCandidate],
    list[dict[str, Any]],
    list[DiagnosticCandidate],
    list[dict[str, str]],
]:
    ordered = sorted(
        candidates,
        key=lambda candidate: _candidate_sort_key(candidate, scope_order),
    )
    suppressed_ids: set[str] = set()
    suppressed: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    # Phase 1 may provide explicit, evidence-bound dependency edges.  They are
    # accepted only after both endpoint diff IDs survived the adapter and can
    # be resolved to different candidates.  A causal edge may suppress an
    # unresolved symptom, but never a candidate with its own atomic proof.
    candidates_by_diff: dict[str, list[DiagnosticCandidate]] = {}
    for candidate in ordered:
        for diff_id in candidate.diff_ids:
            candidates_by_diff.setdefault(diff_id, []).append(candidate)
    for link in dependency_links:
        edge_type = str(link.get("type") or "")
        for parent in candidates_by_diff.get(str(link.get("from_diff_id") or ""), []):
            for child in candidates_by_diff.get(str(link.get("to_diff_id") or ""), []):
                if parent.candidate_id == child.candidate_id:
                    continue
                edges.append(
                    {
                        "from": parent.candidate_id,
                        "to": child.candidate_id,
                        "type": edge_type,
                    }
                )
                if (
                    edge_type in {"CAUSES", "MASKS", "RELOCATES_TO"}
                    and parent.blocking
                    and not child.blocking
                ):
                    suppressed_ids.add(child.candidate_id)

    for parent in ordered:
        for child in ordered:
            if parent.candidate_id == child.candidate_id or parent.scope_id != child.scope_id:
                continue
            shared = set(parent.diff_ids) & set(child.diff_ids)
            if parent.rule_id in {"S1_MISSING_BRIDGE", "S1_CARTESIAN_PRODUCT"} and child.rule_id == "S5_FANOUT_AGGREGATE":
                edges.append({"from": parent.candidate_id, "to": child.candidate_id, "type": "AMPLIFIES"})
                # Aggregate damage remains visible unless it is literally the
                # same evidence node; fan-out and the wrong aggregate can be
                # independent errors.
                if shared and not child.blocking:
                    suppressed_ids.add(child.candidate_id)
            elif (
                parent.rule_id == "S6_ORDER_OFFSET"
                and child.rule_id == "S6_TOPN_WITHOUT_ORDER"
                and shared
            ):
                edges.append({"from": parent.candidate_id, "to": child.candidate_id, "type": "CAUSES"})
                suppressed_ids.add(child.candidate_id)
            elif (
                parent.rule_id.startswith("S3_")
                and child.rule_id == "UNCLASSIFIED_SUPPORTED_DIFF"
                and shared
            ):
                edges.append({"from": parent.candidate_id, "to": child.candidate_id, "type": "CAUSES"})
                suppressed_ids.add(child.candidate_id)
            elif (
                parent.rule_id not in {
                    "UNCLASSIFIED_SUPPORTED_DIFF",
                    "S2_ROW_FILTER_MISMATCH",
                    "S4_GROUP_FILTER_MISMATCH",
                    "S6_RESULT_SHAPING_MISMATCH",
                }
                and child.rule_id in {
                    "UNCLASSIFIED_SUPPORTED_DIFF",
                    "S2_ROW_FILTER_MISMATCH",
                    "S4_GROUP_FILTER_MISMATCH",
                    "S6_RESULT_SHAPING_MISMATCH",
                }
                and shared
                and _EVIDENCE_RANK.get(parent.evidence_grade, 0) >= _EVIDENCE_RANK.get(child.evidence_grade, 0)
            ):
                edges.append({"from": parent.candidate_id, "to": child.candidate_id, "type": "CAUSES"})
                suppressed_ids.add(child.candidate_id)

    for candidate in ordered:
        if candidate.candidate_id in suppressed_ids:
            cause = next((edge["from"] for edge in edges if edge["to"] == candidate.candidate_id), None)
            suppressed.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "rule_id": candidate.rule_id,
                    "cause_candidate_id": cause,
                    "reason": "evidence-backed dependent symptom",
                }
            )

    roots = [item for item in ordered if item.candidate_id not in suppressed_ids and item.blocking]
    unresolved = [item for item in ordered if not item.blocking and item.candidate_id not in suppressed_ids]
    primary = roots[0] if roots else None
    secondary = roots[1:] if roots else []
    edges = [
        {"from": source, "to": target, "type": edge_type}
        for source, target, edge_type in sorted(
            {(item["from"], item["to"], item["type"]) for item in edges}
        )
    ]
    return primary, secondary, suppressed, unresolved, edges


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value[:MAX_PUBLIC_STRING]
    return str(value)[:MAX_PUBLIC_STRING]


def _table_rows(database: Mapping[str, Sequence[Mapping[str, Any]]], table: str) -> Sequence[Mapping[str, Any]]:
    wanted = table.lower()
    for name, rows in database.items():
        if str(name).lower() == wanted:
            return rows
    return ()


def _selected_world(data: Mapping[str, Any]) -> Mapping[str, Any]:
    selected = str(data.get("selected_witness_world_id") or "")
    suite = _mapping(data.get("witness_suite"))
    for raw in _sequence(suite.get("worlds")):
        world = _mapping(raw)
        if str(world.get("world_id") or "") == selected:
            return world
    return {}


def _extract_witness(
    evidence: _Phase1Evidence,
    primary: DiagnosticCandidate | None,
    catalog: Phase2SchemaCatalog,
) -> tuple[dict[str, Any] | None, list[str]]:
    notes: list[str] = []
    if primary is None:
        if evidence.verdict == "INCORRECT":
            return {
                "availability": "OUTPUT_ONLY",
                "cases": [],
                "result_delta": {
                    "expected_only_count": len(_sequence(evidence.data.get("only_in_standard_sample"))),
                    "student_only_count": len(_sequence(evidence.data.get("only_in_student_sample"))),
                },
            }, notes
        return None, notes

    primary_diff_ids = set(primary.diff_ids)
    primary_obligations = set(primary.obligation_ids)
    effects = [
        _mapping(item)
        for item in _sequence(evidence.data.get("obligation_effectiveness"))
        if str(_mapping(item).get("diff_id") or "") in primary_diff_ids
        or str(_mapping(item).get("obligation_id") or "") in primary_obligations
    ]
    effects.sort(
        key=lambda item: (
            0 if item.get("causal_attribution_verified") is True else 1,
            0 if item.get("distinguished") is True else 1,
            str(item.get("diff_id") or ""),
        )
    )
    selected_id = str(evidence.data.get("selected_witness_world_id") or "")
    effect = effects[0] if effects else {}
    effect_world = str(effect.get("world_id") or "")
    if effect and effect_world and selected_id and effect_world != selected_id:
        notes.append("atomic witness world differs from the materialized selected world; input rows withheld")
        return {
            "availability": "OUTPUT_ONLY",
            "cases": [],
            "result_delta": {
                "expected_row_count": len(_sequence(effect.get("standard_result"))),
                "student_row_count": len(_sequence(effect.get("student_result"))),
            },
        }, notes

    world = _selected_world(evidence.data)
    execution = _mapping(world.get("execution"))
    application = _mapping(execution.get("constraint_application"))
    applied = [
        _mapping(item)
        for item in _sequence(application.get("applied"))
        if str(_mapping(item).get("diff_id") or "") in primary_diff_ids
        or str(_mapping(item).get("obligation_id") or "") in primary_obligations
    ]
    if not applied:
        # Some Phase 1 fixtures retain declarative constraints but not the
        # post-materialization row index.  They are not safe physical tuples.
        availability = (
            "PAIR_DISTINGUISHED"
            if effect.get("distinguished") is True
            and effect.get("constraints_satisfied") is not False
            else "OUTPUT_ONLY"
        )
        return {
            "availability": availability,
            "cases": [],
            "result_delta": {
                "expected_row_count": len(_sequence(effect.get("standard_result"))),
                "student_row_count": len(_sequence(effect.get("student_result"))),
            },
        }, notes

    rows_out: list[dict[str, Any]] = []
    cell_budget = MAX_WITNESS_CELLS
    seen_rows: set[tuple[str, int]] = set()
    for item in applied:
        if len(rows_out) >= MAX_WITNESS_ROWS or cell_budget <= 0:
            break
        table = str(item.get("table") or "")
        if not _PUBLIC_IDENTIFIER.fullmatch(table):
            notes.append("unsafe witness table identifier was withheld")
            continue
        try:
            row_index = int(item.get("row_index"))
        except (TypeError, ValueError):
            continue
        key = (table.lower(), row_index)
        if key in seen_rows:
            continue
        rows = _table_rows(evidence.database, table)
        if row_index < 0 or row_index >= len(rows):
            notes.append(f"witness row index out of range for table {table}")
            continue
        row = _mapping(rows[row_index])
        requested_columns = [str(item.get("column") or "")]
        table_fact = catalog.table(table)
        if table_fact is not None:
            requested_columns.extend(table_fact.primary_key)
            requested_columns.extend(
                column
                for foreign_key in table_fact.foreign_keys
                for column in foreign_key.columns
            )
        values: dict[str, Any] = {}
        for name in dict.fromkeys(requested_columns):
            if (
                not name
                or not _PUBLIC_IDENTIFIER.fullmatch(name)
                or name not in row
                or cell_budget <= 0
            ):
                continue
            values[name] = _safe_scalar(row[name])
            cell_budget -= 1
        if not values and cell_budget > 0:
            for name, value in row.items():
                safe_name = str(name)
                if not _PUBLIC_IDENTIFIER.fullmatch(safe_name):
                    continue
                values[safe_name] = _safe_scalar(value)
                cell_budget -= 1
                if cell_budget <= 0:
                    break
        rows_out.append({"table": table, "row_index": row_index, "values": values})
        seen_rows.add(key)

    if not rows_out:
        notes.append("verified obligation could not be linked to a bounded physical row")
        return {
            "availability": "OUTPUT_ONLY",
            "cases": [],
            "result_delta": {
                "expected_row_count": len(_sequence(effect.get("standard_result"))),
                "student_row_count": len(_sequence(effect.get("student_result"))),
            },
        }, notes

    availability = (
        "CAUSAL_VERIFIED"
        if effect.get("causal_attribution_verified") is True
        else "PAIR_DISTINGUISHED"
    )
    case = {
        "case_id": "case_1",
        "world_id": selected_id or effect_world or None,
        "evidence_refs": {
            "diff_ids": sorted(primary_diff_ids),
            "obligation_ids": sorted(primary_obligations),
        },
        "rows": rows_out,
        "expected_row_count": len(_sequence(effect.get("standard_result"))),
        "student_row_count": len(_sequence(effect.get("student_result"))),
    }
    return {"availability": availability, "cases": [case][:MAX_WITNESS_CASES]}, notes


def _schema_facts(catalog: Phase2SchemaCatalog) -> dict[str, Any]:
    facts = catalog.public_facts(max_tables=8, max_columns_per_table=24)
    return {"source": "SCHEMA_CATALOG", **facts}


def _question_text(question: Any) -> str:
    if isinstance(question, Mapping):
        return str(question.get("q") or question.get("content") or "")[:1000]
    return str(question or "")[:1000]


def _first_witness_value(witness: Mapping[str, Any] | None) -> Any:
    for case in _sequence(_mapping(witness).get("cases")):
        for row in _sequence(_mapping(case).get("rows")):
            values = _mapping(_mapping(row).get("values"))
            if values:
                return next(iter(values.values()))
    return None


def _behavior_for_rule(
    rule_id: str,
    *,
    language: str,
    teaching_stage: str,
    title_zh: str,
    title_en: str,
) -> str:
    """Describe the student's current transformation without prescribing a fix.

    These templates deliberately avoid identifiers, literals, operators and
    reference-query fragments.  They make the first narrative slot concrete
    while keeping the repair decision in the learner's hands.
    """

    english = {
        "S1_MISSING_BRIDGE": "The current sources do not form the complete declared relationship path between the requested entities.",
        "S1_CARTESIAN_PRODUCT": "The current query combines rows from multiple sources without an effective relationship constraint.",
        "S1_OUTER_JOIN_MISUSE": "The current join-preservation policy changes whether unmatched entities remain visible.",
        "S1_SUBQUERY_CARDINALITY": "The current comparison treats the inner query as a result shape with a different cardinality.",
        "S2_BOUNDARY": "The row filter currently applies a boundary decision whose open-or-closed behavior differs from the question.",
        "S2_BOOLEAN_LOGIC": "The current grouping of row-filter conditions changes which alternatives receive the shared constraints.",
        "S2_NULL_LOGIC": "The current row filter lets three-valued NULL semantics remove or retain records differently from the requested behavior.",
        "S2_AGGREGATE_IN_WHERE": "The current row-filter stage refers to an aggregate measure before that measure has been formed.",
        "S3_GRAIN_ENTITY_MISMATCH": "The current grouping dimensions make one result row represent a different business entity from the requested one.",
        "S3_GROUP_KEY_MISSING": "The current grouping omits a dimension needed to define the requested result grain.",
        "S3_GROUP_KEY_REDUNDANT": "The current grouping adds a detail dimension that splits the requested entity into several groups.",
        "S4_HAVING_MISSING": "The current query forms group measures but does not apply the requested post-aggregation constraint.",
        "S4_AGG_BOUNDARY": "The group filter currently applies a threshold boundary whose inclusion behavior differs from the question.",
        "S4_ROW_FILTER_IN_HAVING": "The current query postpones a row-decidable condition until after groups have been formed.",
        "S5_FANOUT_AGGREGATE": "The current aggregate consumes repeated joined rows for the same business entity.",
        "S5_COUNT_NULL_SENSITIVITY": "The current count measures a different population because NULL-bearing values and physical rows are treated differently.",
        "S5_CASE_INCOMPLETE": "The current conditional projection leaves at least one possible input path without an explicit outcome.",
        "S5_TOP_LEVEL_DEDUP": "The current final projection can emit the same requested entity more than once.",
        "S6_TOPN_WITHOUT_ORDER": "The current query truncates rows before establishing the deterministic order required by the question.",
        "S6_ORDER_OFFSET": "The current result organization selects a different direction or position at the cutoff.",
    }
    chinese = {
        "S1_MISSING_BRIDGE": "当前查询的数据来源没有形成题目实体之间完整的已声明关系路径。",
        "S1_CARTESIAN_PRODUCT": "当前查询让多个数据来源的记录在缺少有效关系约束时直接组合。",
        "S1_OUTER_JOIN_MISUSE": "当前查询的连接保留策略改变了无匹配实体是否仍然可见。",
        "S1_SUBQUERY_CARDINALITY": "当前查询以不一致的基数形态使用了内层查询的结果。",
        "S2_BOUNDARY": "当前行级筛选采用了与题意不同的开闭边界判断。",
        "S2_BOOLEAN_LOGIC": "当前行级条件的组合方式改变了共同约束所覆盖的备选范围。",
        "S2_NULL_LOGIC": "当前行级筛选受到 NULL 三值逻辑影响，对相关记录作出了与题意不同的保留判断。",
        "S2_AGGREGATE_IN_WHERE": "当前查询在行级筛选阶段引用了尚未形成的聚合度量。",
        "S3_GRAIN_ENTITY_MISMATCH": "当前分组维度使结果中的一行代表了与题目不同的业务实体。",
        "S3_GROUP_KEY_MISSING": "当前分组遗漏了定义题目所需结果粒度的维度。",
        "S3_GROUP_KEY_REDUNDANT": "当前分组加入了明细维度，把题目要求的同一实体拆成了多个分组。",
        "S4_HAVING_MISSING": "当前查询形成了分组度量，但没有施加题目要求的聚合后约束。",
        "S4_AGG_BOUNDARY": "当前组级筛选采用了与题意不同的阈值包含方式。",
        "S4_ROW_FILTER_IN_HAVING": "当前查询把能够逐行判断的条件推迟到了分组形成之后。",
        "S5_FANOUT_AGGREGATE": "当前聚合接收了同一业务实体经关联后产生的重复明细行。",
        "S5_COUNT_NULL_SENSITIVITY": "当前计数因对含 NULL 的值与物理行采用不同口径而统计了另一类对象。",
        "S5_CASE_INCOMPLETE": "当前条件投影至少有一种可能的输入路径没有得到明确结果。",
        "S5_TOP_LEVEL_DEDUP": "当前最终投影可能多次输出同一个题目目标实体。",
        "S6_TOPN_WITHOUT_ORDER": "当前查询在建立题目要求的确定性顺序之前就截断了结果。",
        "S6_ORDER_OFFSET": "当前结果整理在截取边界处选择了不同的方向或位置。",
    }
    if language == "en":
        return english.get(
            rule_id,
            f"Your query changes the data flow at {teaching_stage}, matching {title_en}.",
        )
    text = chinese.get(
        rule_id,
        f"你的查询在 {teaching_stage} 改变了数据流，证据指向“{title_zh}”。",
    )
    if language == "zh-TW":
        for simplified, traditional in (
            ("查询", "查詢"),
            ("数据", "資料"),
            ("实体", "實體"),
            ("关系", "關係"),
            ("连接", "連接"),
            ("记录", "記錄"),
            ("约束", "約束"),
            ("级", "級"),
            ("筛选", "篩選"),
            ("逻辑", "邏輯"),
            ("条件", "條件"),
            ("组合", "組合"),
            ("范围", "範圍"),
            ("业务", "業務"),
            ("维度", "維度"),
            ("结果", "結果"),
            ("题目", "題目"),
            ("分组", "分組"),
            ("聚合", "聚合"),
            ("统计", "統計"),
            ("输出", "輸出"),
            ("顺序", "順序"),
            ("边界", "邊界"),
            ("当前", "當前"),
        ):
            text = text.replace(simplified, traditional)
    return text


def _narrative(
    verdict: str,
    primary: DiagnosticCandidate | None,
    witness: Mapping[str, Any] | None,
    *,
    language: str,
) -> dict[str, str]:
    lang = language if language in {"en", "zh-TW", "zh-CN"} else "zh-CN"
    if verdict == "CORRECT":
        if lang == "en":
            return {
                "student_behavior": "The current bounded checks found no distinguishing counterexample.",
                "conflict_and_witness": "No blocking conflict was established by the available Phase 1 evidence.",
                "guidance_question": "Your submission is operationally accepted for this teaching run; this is not a proof of global equivalence.",
            }
        if lang == "zh-TW":
            return {
                "student_behavior": "目前的有界檢查未找到能區分兩個查詢的反例。",
                "conflict_and_witness": "Phase 1 現有證據沒有建立阻斷性的題意衝突。",
                "guidance_question": "本次作答獲教學性接受；這不代表已證明全域等價。",
            }
        return {
            "student_behavior": "当前有界检查未找到能够区分两个查询的反例。",
            "conflict_and_witness": "Phase 1 现有证据没有建立阻断性的题意冲突。",
            "guidance_question": "本次作答获教学性接受；这不代表已经证明全局等价。",
        }
    if verdict == "UNDECIDED":
        if lang == "en":
            return {
                "student_behavior": "The available bounded evidence is insufficient for a reliable verdict.",
                "conflict_and_witness": "No student fault is asserted because the evidence boundary was reached.",
                "guidance_question": "No learning-state update should be made for this attempt.",
            }
        if lang == "zh-TW":
            return {
                "student_behavior": "目前的有界證據不足以形成可靠判決。",
                "conflict_and_witness": "由於觸及證據邊界，本次不對學生程式碼斷言具體錯因。",
                "guidance_question": "本次嘗試不應寫入錯誤次數或學習狀態。",
            }
        return {
            "student_behavior": "当前有界证据不足以形成可靠判决。",
            "conflict_and_witness": "由于触及证据边界，本次不对学生代码断言具体错因。",
            "guidance_question": "本次尝试不应写入错误次数或学习状态。",
        }
    if primary is None:
        if lang == "en":
            return {
                "student_behavior": "The execution evidence distinguishes this query from the required behavior.",
                "conflict_and_witness": "The current evidence does not safely localize that difference to one teaching rule.",
                "guidance_question": "Which intermediate result first stops matching the question's required data flow?",
            }
        return {
            "student_behavior": "执行证据已经表明当前查询与题目要求存在差异。",
            "conflict_and_witness": "现有证据尚不足以把差异可靠定位到某一条教学规则。",
            "guidance_question": "可以逐阶段检查：哪一个中间结果最先偏离了题目要求的数据流？",
        }

    spec = _RULE_BY_ID.get(primary.rule_id)
    title_zh = spec.title_zh if spec else "尚未分类的逻辑差异"
    title_en = spec.title_en if spec else "an unclassified logical difference"
    value = _first_witness_value(witness)
    availability = str(_mapping(witness).get("availability") or "UNAVAILABLE")
    behavior = _behavior_for_rule(
        primary.rule_id,
        language=lang,
        teaching_stage=primary.teaching_stage,
        title_zh=title_zh,
        title_en=title_en,
    )
    is_boundary_rule = primary.rule_id in {"S2_BOUNDARY", "S4_AGG_BOUNDARY"}
    if lang == "en":
        if is_boundary_rule and value is not None and availability == "CAUSAL_VERIFIED":
            conflict = f"A causally verified witness contains the boundary-relevant value {value!r}; the two behaviors treat that case differently."
        elif availability == "CAUSAL_VERIFIED":
            conflict = "The causally verified witness contains concrete records for which the current transformation and the required behavior produce different outcomes."
        elif availability in {"CAUSAL_VERIFIED", "PAIR_DISTINGUISHED"}:
            conflict = "A bounded witness makes the expected and student result shapes diverge at this point."
        else:
            conflict = "The output differs, but a minimal physical input tuple could not be safely linked."
        guidance = _guidance_for_rule(primary.rule_id, "en")
    elif lang == "zh-TW":
        if is_boundary_rule and value is not None and availability == "CAUSAL_VERIFIED":
            conflict = f"因果物證中存在與邊界有關的值 {value!r}；兩種行為對這個案例產生了不同結果。"
        elif availability == "CAUSAL_VERIFIED":
            conflict = "經因果驗證的物證包含具體記錄；目前轉換與題目要求對這些記錄產生了不同結果。"
        elif availability in {"CAUSAL_VERIFIED", "PAIR_DISTINGUISHED"}:
            conflict = "有界物證顯示，預期結果與學生結果從這個位置開始分歧。"
        else:
            conflict = "輸出已出現差異，但目前無法安全連結到最小實體輸入列。"
        guidance = _guidance_for_rule(primary.rule_id, "zh-TW")
    else:
        if is_boundary_rule and value is not None and availability == "CAUSAL_VERIFIED":
            conflict = f"因果物证中存在与边界有关的值 {value!r}；两种行为对这个案例产生了不同结果。"
        elif availability == "CAUSAL_VERIFIED":
            conflict = "经因果验证的物证包含具体记录；当前转换与题目要求对这些记录产生了不同结果。"
        elif availability in {"CAUSAL_VERIFIED", "PAIR_DISTINGUISHED"}:
            conflict = "有界物证显示，预期结果与学生结果从这个位置开始分歧。"
        else:
            conflict = "输出已经出现差异，但目前无法安全关联到最小物理输入行。"
        guidance = _guidance_for_rule(primary.rule_id, "zh-CN")
    return {
        "student_behavior": behavior,
        "conflict_and_witness": conflict,
        "guidance_question": guidance,
    }


def _guidance_for_rule(rule_id: str, language: str) -> str:
    english = {
        "S1_MISSING_BRIDGE": "Which declared relationship path connects the requested entities, and is every required source present?",
        "S1_CARTESIAN_PRODUCT": "What relationship should constrain how rows from these sources are paired?",
        "S1_OUTER_JOIN_MISUSE": "Which side's entities must remain visible even when no matching row exists?",
        "S1_SUBQUERY_CARDINALITY": "Does the inner query represent one value or a set of values, and what comparison shape matches that result?",
        "S2_BOUNDARY": "Should the boundary value be included or excluded, and which comparison relation expresses that choice?",
        "S2_BOOLEAN_LOGIC": "How should the conditions be grouped so every required constraint applies to the intended alternatives?",
        "S2_NULL_LOGIC": "What truth value does this predicate produce for NULL, and should that row be retained?",
        "S2_AGGREGATE_IN_WHERE": "At which data-flow stage does the aggregate value become available for comparison?",
        "S3_GRAIN_ENTITY_MISMATCH": "What business entity should one output row represent, and does the current grouping dimension identify that entity?",
        "S3_GROUP_KEY_MISSING": "What business entity should one result row represent, and which dimensions define it?",
        "S3_GROUP_KEY_REDUNDANT": "Does every grouping dimension belong to the intended result grain?",
        "S4_HAVING_MISSING": "Which condition can only be evaluated after each group metric has been formed?",
        "S4_AGG_BOUNDARY": "Should a group exactly at the aggregate threshold be retained or excluded?",
        "S4_ROW_FILTER_IN_HAVING": "Can this condition be decided for each input row, or only after a group metric exists?",
        "S5_FANOUT_AGGREGATE": "Before aggregating, can one business entity appear in several joined rows?",
        "S5_COUNT_NULL_SENSITIVITY": "Are you counting physical rows or only non-NULL values?",
        "S5_CASE_INCOMPLETE": "What should happen when none of the listed branches matches?",
        "S5_TOP_LEVEL_DEDUP": "Should one output entity be allowed to appear more than once?",
        "S6_TOPN_WITHOUT_ORDER": "Which total ordering must be established before rows are truncated?",
        "S6_ORDER_OFFSET": "After defining ties, what direction and zero-based position select the intended row?",
    }
    chinese = {
        "S1_MISSING_BRIDGE": "想一想：题目中的实体应通过哪条已声明关系路径连接，所需的数据来源是否齐全？",
        "S1_CARTESIAN_PRODUCT": "想一想：来自不同数据源的记录应受哪种关系约束后再进行配对？",
        "S1_OUTER_JOIN_MISUSE": "想一想：哪一侧的业务实体即使没有匹配记录也必须保留？",
        "S1_SUBQUERY_CARDINALITY": "想一想：内层查询表达的是单个值还是一组值，外层应采用哪类比较关系？",
        "S2_BOUNDARY": "想一想：临界值应被包含还是排除，哪种比较关系能表达这个开闭边界？",
        "S2_BOOLEAN_LOGIC": "想一想：条件应如何分组，才能让共同约束作用于题意中的全部备选情况？",
        "S2_NULL_LOGIC": "想一想：当前谓词遇到 NULL 时得到什么真值，这类记录是否应被保留？",
        "S2_AGGREGATE_IN_WHERE": "想一想：聚合度量要到数据流的哪个阶段才形成并可用于比较？",
        "S3_GRAIN_ENTITY_MISMATCH": "想一想：结果中的一行代表哪个业务实体，当前维度能否唯一刻画它？",
        "S3_GROUP_KEY_MISSING": "想一想：结果中的一行代表哪个业务实体，哪些维度共同定义它？",
        "S3_GROUP_KEY_REDUNDANT": "想一想：每一个分组维度都属于题目要求的结果粒度吗？",
        "S4_HAVING_MISSING": "想一想：哪项条件必须等每个分组的度量形成后才能判断？",
        "S4_AGG_BOUNDARY": "想一想：恰好落在聚合阈值上的分组应被保留还是排除？",
        "S4_ROW_FILTER_IN_HAVING": "想一想：这个条件能否逐行判断，还是必须等分组度量形成后才能判断？",
        "S5_FANOUT_AGGREGATE": "想一想：聚合之前，同一个业务实体是否可能因关联而出现多行？",
        "S5_COUNT_NULL_SENSITIVITY": "想一想：题目要统计物理行数，还是只统计非空有效值？",
        "S5_CASE_INCOMPLETE": "想一想：没有任何已列分支命中时，结果应该如何处理？",
        "S5_TOP_LEVEL_DEDUP": "想一想：同一个输出实体是否允许重复出现？",
        "S6_TOPN_WITHOUT_ORDER": "想一想：截断结果之前，必须先建立怎样的确定性顺序？",
        "S6_ORDER_OFFSET": "想一想：明确并列语义后，怎样的方向和从零起算的位置才对应目标项？",
    }
    fallback_en = "Which data-flow decision at this stage should be reconsidered before changing later clauses?"
    fallback_zh = "想一想：在修改下游子句前，这个阶段的哪项数据流决策需要重新核对？"
    if language == "en":
        return english.get(rule_id, fallback_en)
    text = chinese.get(rule_id, fallback_zh)
    if language == "zh-TW":
        return text.replace("想一想", "想一想").replace("结果", "結果").replace("实体", "實體").replace("关联", "關聯").replace("统计", "統計")
    return text


_FORBIDDEN_PUBLIC_KEYS = {
    "answer_sql",
    "correct_sql",
    "standard_sql",
    "standard_node",
    "standard_fragment",
    "standard_result",
    "standard_sample_rows",
    "replacement_sql",
    "mutation_sql",
    "mutant_sql",
    "variant_sql",
    "test_database",
    "witness_world",
    "raw_observation",
    "llm_arbitration_input",
}


def _normalized_secret(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().rstrip(";")).lower()


def sanitize_public_package(
    payload: Mapping[str, Any],
    *,
    forbidden_values: Iterable[str] = (),
) -> dict[str, Any]:
    """Recursively bound and redact a learner-facing package.

    This is a last line of defence, not the mechanism used to build the public
    DTO.  Forbidden keys are removed entirely; an accidental full reference
    SQL value is redacted.  Bounded iteration prevents a malformed diagnostic
    object from becoming a memory amplifier.
    """

    secrets: set[str] = set()
    for value in islice(forbidden_values, MAX_FORBIDDEN_VALUES):
        if not isinstance(value, str):
            continue
        normalized = _normalized_secret(value[:MAX_FORBIDDEN_VALUE_CHARS])
        if len(normalized) >= 8:
            secrets.add(normalized)
    node_budget = 4096

    def clean(value: Any, depth: int = 0) -> Any:
        nonlocal node_budget
        node_budget -= 1
        if node_budget < 0 or depth > 12:
            return "[truncated]"
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for index, (key_raw, nested) in enumerate(value.items()):
                if index >= 256:
                    result["_truncated"] = True
                    break
                key = str(key_raw)
                if key.lower() in _FORBIDDEN_PUBLIC_KEYS:
                    continue
                result[key] = clean(nested, depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [clean(item, depth + 1) for item in value[:256]]
        if isinstance(value, str):
            text = value[:2000]
            normalized = _normalized_secret(text)
            if any(secret in normalized for secret in secrets):
                return "[redacted reference content]"
            return text
        return _safe_scalar(value)

    cleaned = clean(payload)
    return cleaned if isinstance(cleaned, dict) else {}


def _forbidden_reference_values(evidence: _Phase1Evidence) -> tuple[str, ...]:
    values: list[str] = []
    if evidence.standard_sql:
        values.append(evidence.standard_sql[:MAX_FORBIDDEN_VALUE_CHARS])
    # Phase 1 rich diffs contain reference fragments.  They are useful inside
    # the adapter but must not be copied into the learner package.
    for item in _sequence(evidence.data.get("ast_diffs"))[:MAX_ORDERED_DIFFS]:
        raw = _mapping(item)
        for key in (
            "standard_query_sql",
            "standard_sql",
            "standard_node",
            "standard_fragment",
        ):
            standard = raw.get(key)
            if isinstance(standard, str) and standard:
                values.append(standard[:MAX_FORBIDDEN_VALUE_CHARS])
    return tuple(dict.fromkeys(values))


def _build_qss(
    question: Any,
    catalog: Phase2SchemaCatalog,
    primary: DiagnosticCandidate | None,
    witness: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "question": {
            "source": "QUESTION",
            "intent": _question_text(question),
            "confidence": "DECLARED" if _question_text(question) else "UNKNOWN",
        },
        "schema": _schema_facts(catalog),
        "student_behavior": {
            "source": "PHASE1_EVIDENCE",
            "scope_id": primary.scope_id if primary else None,
            "stage": primary.teaching_stage if primary else None,
            "witness_availability": _mapping(witness).get("availability") if witness else "UNAVAILABLE",
        },
    }


def diagnose_record(
    record: Any = None,
    *,
    sandbox_run: Any = None,
    attribution_result: Any = None,
    question: Any = "",
    schema: Any = None,
    student_sql: str = "",
    language: str = "zh-CN",
) -> DiagnosticPackage:
    """Build a deterministic Phase 2 package from one Phase 1 run.

    ``attribution_result`` is accepted as an optional compatibility input.  It
    may enrich future ranking, but it is intentionally not authoritative and a
    missing/failed attribution layer does not prevent diagnosis from the rich
    Phase 1 evidence.
    """

    del attribution_result  # Explicitly non-authoritative in Phase 2 v1.
    if sandbox_run is None:
        sandbox_run = _field(record, "sandbox_run") if record is not None else None
    if sandbox_run is None and record is not None and _field(record, "data_evidence") is not None:
        sandbox_run = record
    if sandbox_run is None:
        raise ValueError("diagnose_record requires a Phase 1 SandboxRun")

    evidence = _adapt_phase1(sandbox_run)
    if student_sql:
        evidence.student_sql = student_sql
    # Parse once through the bounded adapter.  Do not JSON-decode an unbounded
    # string in this facade before the catalog has enforced its byte limit.
    schema_catalog = parse_schema_catalog(schema)
    forbidden_values = _forbidden_reference_values(evidence)

    if evidence.verdict == "CORRECT":
        package = DiagnosticPackage(
            verdict="CORRECT",
            diagnosis_status="OPERATIONALLY_ACCEPTED",
            phase1_status=evidence.phase1_status,
            equivalence_conclusion=evidence.equivalence_conclusion,
            judge_status=evidence.judge_status,
            ordered_pipeline=[],
            primary=None,
            witness=None,
            qss=_build_qss(question, schema_catalog, None, None),
            narrative=_narrative("CORRECT", None, None, language=language),
            boundary_notes=[
                "No counterexample was found within the current bounded Phase 1 checks; global equivalence is not claimed."
            ],
            schema_catalog=schema_catalog.public_facts(),
            _forbidden_values=forbidden_values,
        )
        package.to_dict()  # Exercise the public sanitizer before returning.
        return package

    if evidence.verdict == "UNDECIDED":
        package = DiagnosticPackage(
            verdict="UNDECIDED",
            diagnosis_status="UNDECIDED",
            phase1_status=evidence.phase1_status,
            equivalence_conclusion=evidence.equivalence_conclusion,
            judge_status="UNDECIDED",
            ordered_pipeline=[],
            primary=None,
            witness=None,
            qss=_build_qss(question, schema_catalog, None, None),
            narrative=_narrative("UNDECIDED", None, None, language=language),
            boundary_notes=[
                "Phase 1 did not produce a teachable verdict; no fault attribution is asserted."
            ],
            schema_catalog=schema_catalog.public_facts(),
            _forbidden_values=forbidden_values,
        )
        package.to_dict()
        return package

    scope_graph = build_scoped_query_graph(sandbox_run)
    scope_order = _apply_scoped_query_graph(evidence, scope_graph)
    candidates = _detect_candidates(evidence, catalog=schema_catalog)
    primary, secondary, suppressed, unresolved, edges = _bundle_and_rank(
        candidates,
        dependency_links=evidence.dependency_links,
        scope_order=scope_order,
    )
    witness, witness_notes = _extract_witness(evidence, primary, schema_catalog)
    notes = [
        *evidence.limitations,
        *(f"SchemaCatalog: {item}" for item in schema_catalog.limitations),
        *witness_notes,
    ]
    if scope_graph.limitations:
        notes.append(
            "ScopedQueryGraph is PARTIAL; bounded scope details remain in the internal audit trace."
        )
    if primary is None:
        notes.append(
            "The query pair is distinguished, but no MVP candidate has sufficient atomic causal evidence."
        )
    if any(item.rule_id == "UNCLASSIFIED_SUPPORTED_DIFF" for item in candidates):
        notes.append(
            "At least one supported difference is outside the 20-rule MVP catalogue and remains explicitly unclassified."
        )
    package = DiagnosticPackage(
        verdict="INCORRECT",
        # Scope-graph limitations qualify *where* the fault belongs; they do
        # not erase an independently causal/repair-verified rule diagnosis.
        diagnosis_status="SUPPORTED" if primary is not None else "PARTIAL",
        phase1_status=evidence.phase1_status,
        equivalence_conclusion=evidence.equivalence_conclusion,
        judge_status=evidence.judge_status,
        ordered_pipeline=evidence.diffs,
        primary=primary,
        secondary=secondary,
        suppressed=suppressed,
        unresolved=unresolved,
        witness=witness,
        qss=_build_qss(question, schema_catalog, primary, witness),
        narrative=_narrative("INCORRECT", primary, witness, language=language),
        boundary_notes=notes,
        causal_edges=edges,
        candidate_trace=candidates,
        scoped_query_graph=scope_graph.to_dict(),
        schema_catalog=schema_catalog.public_facts(),
        _forbidden_values=forbidden_values,
    )
    package.to_dict()
    return package


def render_diagnostic_feedback(
    package: DiagnosticPackage | Mapping[str, Any],
    *,
    language: str = "zh-CN",
) -> str:
    """Render only the already-validated three-slot public narrative."""

    payload = package.to_dict() if isinstance(package, DiagnosticPackage) else sanitize_public_package(package)
    narrative = _mapping(payload.get("narrative"))
    first = str(narrative.get("student_behavior") or "").strip()
    second = str(narrative.get("conflict_and_witness") or "").strip()
    third = str(narrative.get("guidance_question") or "").strip()
    if language == "en":
        labels = ("What your query does", "Conflict and witness", "Question to consider")
    elif language == "zh-TW":
        labels = ("你的查詢目前做了什麼", "衝突與物證", "請思考")
    else:
        labels = ("你的查询目前做了什么", "冲突与物证", "请思考")
    return "\n\n".join(
        f"{index}. {label}：{text}"
        for index, (label, text) in enumerate(zip(labels, (first, second, third)), start=1)
        if text
    )


def _valid_llm_narrative(
    candidate: Any,
    *,
    forbidden_values: Iterable[str],
    required_guidance: str,
) -> dict[str, str] | None:
    if not isinstance(candidate, Mapping):
        return None
    # A renderer may return {narrative: {...}} but no other package fields.  A
    # full-package response is rejected instead of ignoring attempted verdict
    # or evidence-ID edits.
    if "narrative" in candidate:
        if set(candidate) != {"narrative"}:
            return None
        candidate = candidate.get("narrative")
    if not isinstance(candidate, Mapping):
        return None
    required = {"student_behavior", "conflict_and_witness", "guidance_question"}
    if set(candidate) != required:
        return None
    result: dict[str, str] = {}
    secrets = {_normalized_secret(item) for item in forbidden_values if isinstance(item, str) and len(item.strip()) >= 8}
    for key in required:
        value = candidate.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 800:
            return None
        normalized = _normalized_secret(value)
        if any(secret and secret in normalized for secret in secrets):
            return None
        if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|WITH)\b[\s\S]{0,200}\b(FROM|SET|AS)\b", value, flags=re.IGNORECASE):
            return None
        if re.search(
            r"(?:\b(?:WHERE|HAVING|ON|LIMIT|OFFSET)\b\s+[A-Za-z_\"`]|"
            r"[A-Za-z_][A-Za-z0-9_.\"`]*\s*(?:<>|!=|<=|>=|=|<|>))",
            value,
            flags=re.IGNORECASE,
        ):
            return None
        result[key] = value.strip()
    guidance = result["guidance_question"]
    # The third slot carries the actual pedagogical direction.  Keep it
    # deterministic so an optional renderer cannot turn a Socratic question
    # into a repair instruction or smuggle a reference-only predicate.
    if guidance != required_guidance:
        return None
    if "```" in guidance or ";" in guidance:
        return None
    return result


def diagnose_record_with_llm(
    record: Any = None,
    *,
    renderer: Callable[[dict[str, Any]], Any] | None = None,
    llm_renderer: Callable[[dict[str, Any]], Any] | None = None,
    **kwargs: Any,
) -> DiagnosticPackage:
    """Optionally render narrative text without delegating verdict or facts.

    The callback receives only the sanitized public package.  It may return
    exactly three narrative strings.  Any exception, async result, unexpected
    key, SQL-shaped answer or secret value causes a deterministic fallback.
    """

    package = diagnose_record(record, **kwargs)
    callback = renderer or llm_renderer
    if callback is None or package.verdict == "UNDECIDED":
        return package
    try:
        rendered = callback(package.to_dict())
        if inspect.isawaitable(rendered):
            return package
        narrative = _valid_llm_narrative(
            rendered,
            forbidden_values=package._forbidden_values,
            required_guidance=package.narrative.get("guidance_question", ""),
        )
    except Exception:
        return package
    if narrative is None:
        return package
    updated = replace(package, narrative=narrative)
    updated.to_dict()
    return updated


__all__ = [
    "DIAGNOSIS_VERSION",
    "DiagnosticCandidate",
    "DiagnosticPackage",
    "EvidenceDiff",
    "INTERNAL_SCHEMA_VERSION",
    "LOGICAL_STAGE_ORDER",
    "PUBLIC_SCHEMA_VERSION",
    "RULE_CATALOG",
    "RULE_CATALOG_VERSION",
    "RuleSpec",
    "diagnose_record",
    "diagnose_record_with_llm",
    "render_diagnostic_feedback",
    "sanitize_public_package",
]
