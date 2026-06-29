"""阶段 1/2：SQL 错误证据采集与知识点归因。

本模块对应闭环图中的：
- Observe: E_AST / E_data / Mutation Testing / LLM Arbitration Input
- Diagnosis: evidence_weights_from_observation -> KP 错因

目标是先给出稳定、可解释、可测试的知识点级错误归因。这里不直接调用 LLM，
而是把 Intended KP / Observed KP / mismatch evidence 打包出来，后续可作为
阶段 2 LLM attribution input 或提示生成上下文。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import sqlglot
from sqlglot import ErrorLevel, exp


@dataclass
class ASTError:
    """Local structural error shape consumed by BKT and ActionSelector.

    It intentionally mirrors core.ast_analyzer.ASTError without importing that
    module, because ast_analyzer initializes AI settings for LLM filtering.
    Phase 1/2 offline evidence packaging must not require backend .env values.
    """

    error_type: str
    clause: str
    knowledge_point_id: str
    severity: float
    detail: str


@dataclass
class EvidenceItem:
    """单条可解释证据。"""

    source: str
    signal: str
    detail: str
    weight: float


@dataclass
class KPAttribution:
    """知识点层级错因归因。"""

    knowledge_point_id: str
    l1_code: str
    l2_code: str
    clause: str
    error_type: str
    severity: float
    confidence: float
    detail: str
    evidence: list[EvidenceItem] = field(default_factory=list)

    def to_ast_error(self) -> ASTError:
        """转换为现有 BKT / ActionSelector 可消费的 ASTError。"""
        return ASTError(
            error_type=self.error_type,
            clause=self.clause,
            knowledge_point_id=self.knowledge_point_id,
            severity=self.severity,
            detail=self.detail,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [asdict(item) for item in self.evidence]
        return data


@dataclass
class AttributionResult:
    """阶段 1/2 的完整输出。"""

    observation: dict[str, Any]
    attributions: list[KPAttribution]
    llm_arbitration_input: dict[str, Any]

    def to_ast_errors(self) -> list[ASTError]:
        return [item.to_ast_error() for item in self.attributions]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation": self.observation,
            "attributions": [item.to_dict() for item in self.attributions],
            "llm_arbitration_input": self.llm_arbitration_input,
        }


KP_META: dict[str, dict[str, str]] = {
    "select-basic": {"l1": "KP_BASIC", "l2": "PROJ_COL", "clause": "SELECT"},
    "where": {"l1": "KP_FILTER", "l2": "COMP_VAL", "clause": "WHERE"},
    "order-by": {"l1": "KP_ORDER", "l2": "SORT_ASC", "clause": "ORDER BY"},
    "limit": {"l1": "KP_BASIC", "l2": "LIMIT_OFF", "clause": "LIMIT"},
    "distinct": {"l1": "KP_BASIC", "l2": "DISTINCT_SET", "clause": "DISTINCT"},
    "alias": {"l1": "KP_BASIC", "l2": "ALIAS_COL", "clause": "AS"},
    "agg-count": {"l1": "KP_AGG", "l2": "AGG_BASIC", "clause": "AGGREGATION"},
    "group-by": {"l1": "KP_AGG", "l2": "GB_SIMPLE", "clause": "GROUP BY"},
    "having": {"l1": "KP_AGG", "l2": "HV_SIMPLE", "clause": "HAVING"},
    "join-inner": {"l1": "KP_JOIN", "l2": "JOIN_INNER", "clause": "JOIN"},
    "join-on": {"l1": "KP_JOIN", "l2": "JOIN_ON", "clause": "JOIN ON"},
    "join-left": {"l1": "KP_JOIN", "l2": "JOIN_LEFT", "clause": "LEFT JOIN"},
    "join-right": {"l1": "KP_JOIN", "l2": "JOIN_RIGHT", "clause": "RIGHT JOIN"},
    "join-full": {"l1": "KP_JOIN", "l2": "JOIN_FULL", "clause": "FULL JOIN"},
    "subquery-scalar": {"l1": "KP_SUBQUERY", "l2": "SUB_TABLE", "clause": "SUBQUERY"},
    "subquery-in": {"l1": "KP_SUBQUERY", "l2": "SUB_IN_ALL_ANY", "clause": "IN SUBQUERY"},
    "subquery-exists": {"l1": "KP_SUBQUERY", "l2": "SUB_EXISTS", "clause": "EXISTS"},
    "cte": {"l1": "KP_ADVANCED", "l2": "CTE_SIMPLE", "clause": "WITH"},
    "union": {"l1": "KP_ADVANCED", "l2": "SET_UNION", "clause": "UNION"},
    "window-row-number": {"l1": "KP_ADVANCED", "l2": "WIN_OVER", "clause": "WINDOW"},
    "case": {"l1": "KP_FUNC", "l2": "CASE_SEARCH", "clause": "CASE"},
}

COMPARISON_NODE_TYPES = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.In, exp.Between, exp.Is)
AGG_NODE_TYPES = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
COMPLEXITY_KPS = {
    "has_subquery": "subquery-scalar",
    "has_cte": "cte",
    "has_window": "window-row-number",
    "has_union": "union",
    "has_case": "case",
}


def _parse(sql: str) -> exp.Expression | None:
    for dialect in ("tsql", "sqlite", "mysql"):
        try:
            parsed = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.IGNORE)
            if parsed is not None:
                return parsed
        except Exception:
            continue
    return None


def _nodes(ast: exp.Expression | None, *types: type[exp.Expression]) -> list[exp.Expression]:
    if ast is None:
        return []
    return list(ast.find_all(*types))


def _first(ast: exp.Expression | None, node_type: type[exp.Expression]) -> exp.Expression | None:
    if ast is None:
        return None
    return ast.find(node_type)


def _node_sql(node: exp.Expression | None) -> str:
    if node is None:
        return ""
    try:
        return node.sql(dialect="mysql", normalize=True)
    except Exception:
        return str(node)


def _join_sides(ast: exp.Expression | None) -> list[str]:
    sides: list[str] = []
    for join in _nodes(ast, exp.Join):
        side = str(join.args.get("side") or join.args.get("kind") or "INNER").upper()
        sides.append(side if side else "INNER")
    return sides


def _has_join_on(ast: exp.Expression | None) -> bool:
    return any(bool(join.args.get("on")) for join in _nodes(ast, exp.Join))


def _agg_names(ast: exp.Expression | None) -> set[str]:
    return {type(node).__name__.upper() for node in _nodes(ast, *AGG_NODE_TYPES)}


def _select_projection_count(ast: exp.Expression | None) -> int:
    select = _first(ast, exp.Select)
    if not isinstance(select, exp.Select):
        return 0
    return len(select.expressions or [])


def _features(ast: exp.Expression | None) -> dict[str, Any]:
    return {
        "parse_ok": ast is not None,
        "has_select": _first(ast, exp.Select) is not None,
        "has_where": _first(ast, exp.Where) is not None,
        "has_order": _first(ast, exp.Order) is not None,
        "has_limit": _first(ast, exp.Limit) is not None or _first(ast, exp.Offset) is not None,
        "has_distinct": _first(ast, exp.Distinct) is not None,
        "has_group": _first(ast, exp.Group) is not None,
        "has_having": _first(ast, exp.Having) is not None,
        "has_agg": bool(_agg_names(ast)),
        "has_subquery": bool(_nodes(ast, exp.Subquery)),
        "has_cte": bool(_nodes(ast, exp.CTE)),
        "has_union": bool(_nodes(ast, exp.Union)),
        "has_window": bool(_nodes(ast, exp.Window)),
        "has_case": bool(_nodes(ast, exp.Case)),
        "join_count": len(_nodes(ast, exp.Join)),
        "join_sides": _join_sides(ast),
        "has_join_on": _has_join_on(ast),
        "agg_functions": sorted(_agg_names(ast)),
        "projection_count": _select_projection_count(ast),
    }


def _clause_node(ast: exp.Expression | None, node_type: type[exp.Expression]) -> exp.Expression | None:
    return _first(ast, node_type)


def _node_has(node: exp.Expression | None, *types: type[exp.Expression]) -> bool:
    return bool(node and (isinstance(node, types) or any(True for _ in node.find_all(*types))))


def _node_items_sql(node: exp.Expression | None, *types: type[exp.Expression]) -> list[str]:
    if node is None:
        return []
    items: list[exp.Expression] = []
    if isinstance(node, types):
        items.append(node)
    items.extend(list(node.find_all(*types)))
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        sql = _node_sql(item)
        if sql and sql not in seen:
            seen.add(sql)
            out.append(sql)
    return out


def _comparison_locations(ast: exp.Expression | None) -> dict[str, list[str]]:
    if ast is None:
        return {}
    locations: dict[str, list[str]] = {}
    where = _clause_node(ast, exp.Where)
    having = _clause_node(ast, exp.Having)
    for name, node in [("WHERE", where), ("HAVING", having)]:
        items = _node_items_sql(node, *COMPARISON_NODE_TYPES)
        if items:
            locations[name] = items
    select_items: list[str] = []
    select = _first(ast, exp.Select)
    if isinstance(select, exp.Select):
        for expression in select.expressions or []:
            select_items.extend(_node_items_sql(expression, *COMPARISON_NODE_TYPES))
    if select_items:
        locations["SELECT"] = list(dict.fromkeys(select_items))
    join_items: list[str] = []
    for join in _nodes(ast, exp.Join):
        join_items.extend(_node_items_sql(join.args.get("on"), *COMPARISON_NODE_TYPES))
    if join_items:
        locations["JOIN ON"] = join_items
    return locations


def _aggregate_locations(ast: exp.Expression | None) -> dict[str, list[str]]:
    if ast is None:
        return {}
    locations: dict[str, list[str]] = {}
    for name, node in [
        ("WHERE", _clause_node(ast, exp.Where)),
        ("HAVING", _clause_node(ast, exp.Having)),
        ("ORDER BY", _clause_node(ast, exp.Order)),
    ]:
        items = _node_items_sql(node, *AGG_NODE_TYPES)
        if items:
            locations[name] = items
    select_items: list[str] = []
    select = _first(ast, exp.Select)
    if isinstance(select, exp.Select):
        for expression in select.expressions or []:
            select_items.extend(_node_items_sql(expression, *AGG_NODE_TYPES))
    if select_items:
        locations["SELECT"] = list(dict.fromkeys(select_items))
    return locations


def _structural_kps(features: dict[str, Any]) -> list[str]:
    kps = ["select-basic"] if features.get("has_select") else []
    for kp_id, key in [
        ("where", "has_where"),
        ("order-by", "has_order"),
        ("limit", "has_limit"),
        ("distinct", "has_distinct"),
        ("agg-count", "has_agg"),
        ("group-by", "has_group"),
        ("having", "has_having"),
        ("subquery-scalar", "has_subquery"),
        ("cte", "has_cte"),
        ("union", "has_union"),
        ("window-row-number", "has_window"),
        ("case", "has_case"),
    ]:
        if features.get(key):
            kps.append(kp_id)
    if features.get("join_count", 0) > 0:
        kps.append("join-inner")
    if features.get("has_join_on"):
        kps.append("join-on")
    for side, kp_id in [("LEFT", "join-left"), ("RIGHT", "join-right"), ("FULL", "join-full")]:
        if side in features.get("join_sides", []):
            kps.append(kp_id)
    return list(dict.fromkeys(kps))


def _clause_sql_map(ast: exp.Expression | None) -> dict[str, str]:
    if ast is None:
        return {}
    clauses = {
        "WHERE": _first(ast, exp.Where),
        "GROUP BY": _first(ast, exp.Group),
        "HAVING": _first(ast, exp.Having),
        "ORDER BY": _first(ast, exp.Order),
        "LIMIT": _first(ast, exp.Limit) or _first(ast, exp.Offset),
    }
    out = {name: _node_sql(node) for name, node in clauses.items() if node is not None}
    select = _first(ast, exp.Select)
    if isinstance(select, exp.Select):
        projection_sql = ", ".join(_node_sql(item) for item in select.expressions or [])
        if projection_sql:
            out["SELECT"] = projection_sql
    join_on = [_node_sql(join.args.get("on")) for join in _nodes(ast, exp.Join) if join.args.get("on")]
    if join_on:
        out["JOIN ON"] = " | ".join(join_on)
    return out


def _kp_profile(
    *,
    role: str,
    ast: exp.Expression | None,
    features: dict[str, Any],
    question_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    question_context = question_context or {}
    aggregate_locations = _aggregate_locations(ast)
    illegal_aggregate_locations = [name for name in aggregate_locations if name == "WHERE"]
    return {
        "role": role,
        "question_l1": question_context.get("l1") if role == "intended" else None,
        "question_l2": question_context.get("l2") if role == "intended" else None,
        "structural_kps": _structural_kps(features),
        "features": features,
        "clause_sql": _clause_sql_map(ast),
        "comparison_locations": _comparison_locations(ast),
        "aggregate_locations": aggregate_locations,
        "illegal_aggregate_locations": illegal_aggregate_locations,
        "joins_have_on": features.get("join_count", 0) == 0 or features.get("has_join_on"),
        "complexity_kps": [kp for key, kp in COMPLEXITY_KPS.items() if features.get(key)],
    }


def _add_misalignment_evidence(
    builder: _AttributionBuilder,
    intended: dict[str, Any],
    observed: dict[str, Any],
    judge: dict[str, Any],
    is_correct: bool,
) -> list[dict[str, Any]]:
    """Bidirectional Target KP vs Observed KP comparison."""

    misalignments: list[dict[str, Any]] = []
    target_kps = set(intended.get("structural_kps") or [])
    observed_kps = set(observed.get("structural_kps") or [])

    def add(
        *,
        category: str,
        kp_id: str,
        detail: str,
        source: str,
        signal: str,
        weight: float,
        error_type: str | None = None,
        clause: str | None = None,
    ) -> None:
        meta = KP_META.get(kp_id, {})
        misalignments.append({
            "category": category,
            "knowledge_point_id": kp_id,
            "l1_code": meta.get("l1"),
            "l2_code": meta.get("l2"),
            "clause": clause or meta.get("clause"),
            "detail": detail,
            "source": source,
            "signal": signal,
            "weight": round(weight, 3),
        })
        builder.add(kp_id, error_type or category.lower(), source, signal, detail, weight)

    for kp_id in sorted(target_kps - observed_kps):
        if kp_id == "select-basic":
            continue
        add(
            category="Lacking",
            kp_id=kp_id,
            detail=f"目标知识点包含 {kp_id}，但学生 SQL 没有对应结构。",
            source="E_AST",
            signal=f"target_missing:{kp_id}",
            weight=0.88,
            error_type="lacking",
        )

    intended_cmp = intended.get("comparison_locations") or {}
    observed_cmp = observed.get("comparison_locations") or {}
    for src, dst, kp_id in [("HAVING", "WHERE", "having"), ("WHERE", "HAVING", "where")]:
        if intended_cmp.get(src) and observed_cmp.get(dst):
            add(
                category="Confusion",
                kp_id=kp_id,
                detail=f"比较条件目标位置在 {src}，学生写在 {dst}，属于子句职责错位。",
                source="E_AST",
                signal=f"clause_confusion:{src}_vs_{dst}",
                weight=0.82,
                error_type="confusion",
                clause=f"{src}/{dst}",
            )

    if intended.get("features", {}).get("has_agg") and observed.get("illegal_aggregate_locations"):
        add(
            category="Confusion",
            kp_id="having" if intended.get("features", {}).get("has_having") else "agg-count",
            detail="学生把聚合函数放在 WHERE 等非法或不合适的位置，混淆了行过滤与分组后过滤。",
            source="E_AST",
            signal="aggregate_illegal_location",
            weight=0.86,
            error_type="confusion",
        )

    if observed.get("features", {}).get("join_count", 0) > 0 and not observed.get("features", {}).get("has_join_on"):
        add(
            category="Confusion",
            kp_id="join-on",
            detail="学生使用了 JOIN，但没有写 ON 连接条件，连接结构存在职责缺口。",
            source="E_AST",
            signal="join_without_on",
            weight=0.95,
            error_type="confusion",
        )

    intended_clauses = intended.get("clause_sql") or {}
    observed_clauses = observed.get("clause_sql") or {}
    clause_to_kp = {
        "WHERE": "where",
        "GROUP BY": "group-by",
        "HAVING": "having",
        "ORDER BY": "order-by",
        "LIMIT": "limit",
        "JOIN ON": "join-on",
    }
    for clause, kp_id in clause_to_kp.items():
        if intended_clauses.get(clause) and observed_clauses.get(clause) and intended_clauses[clause] != observed_clauses[clause]:
            add(
                category="Logical",
                kp_id=kp_id,
                detail=f"{clause} 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
                source="E_MUT",
                signal=f"same_clause_mismatch:{clause}",
                weight=0.66,
                error_type="logical",
                clause=clause,
            )

    generated_equiv = judge.get("is_equivalent_on_generated_data")
    if is_correct and generated_equiv is False:
        for kp_id in sorted((target_kps & observed_kps) or target_kps or {"where"}):
            add(
                category="Generality",
                kp_id=kp_id,
                detail="原始判题数据正确，但动态生成的反例数据暴露出泛化错误。",
                source="E_data",
                signal="counterexample_failed",
                weight=0.78,
                error_type="generality",
            )

    extra_complexity = sorted(set(observed.get("complexity_kps") or []) - set(intended.get("complexity_kps") or []))
    if is_correct and extra_complexity:
        for kp_id in extra_complexity:
            add(
                category="Complication",
                kp_id=kp_id,
                detail=f"结果正确，但学生使用了目标不需要的 {kp_id} 复杂结构，可归因为不必要复杂化。",
                source="E_AST",
                signal=f"unnecessary_complexity:{kp_id}",
                weight=0.42,
                error_type="complication",
            )

    return misalignments


def _judge_features(judge_detail: dict[str, Any] | None, error_message: str | None) -> dict[str, Any]:
    detail = judge_detail or {}
    comparison = detail.get("comparison") or {}
    return {
        "is_correct": bool(detail.get("is_correct", False)),
        "error_message": detail.get("error_message") or error_message,
        "student_rows": (detail.get("student_result_meta") or {}).get("row_count"),
        "correct_rows": (detail.get("correct_result_meta") or {}).get("row_count"),
        "student_columns": (detail.get("student_result_meta") or {}).get("columns"),
        "correct_columns": (detail.get("correct_result_meta") or {}).get("columns"),
        "ordered_compare": comparison.get("ordered"),
        "alias_enforced": comparison.get("enforce_aliases"),
        "sandbox_executed": comparison.get("sandbox_executed"),
        "sandbox_error": comparison.get("sandbox_error"),
        "is_equivalent_on_generated_data": detail.get("is_equivalent_on_generated_data") or comparison.get("is_equivalent_on_generated_data"),
    }


class _AttributionBuilder:
    def __init__(self) -> None:
        self._items: dict[str, KPAttribution] = {}

    def add(self, kp_id: str, error_type: str, source: str, signal: str, detail: str, weight: float) -> None:
        meta = KP_META.get(kp_id, {"l1": "KP_BASIC", "l2": kp_id.upper(), "clause": kp_id.upper()})
        item = self._items.get(kp_id)
        evidence = EvidenceItem(source=source, signal=signal, detail=detail, weight=round(weight, 3))
        if item is None:
            item = KPAttribution(
                knowledge_point_id=kp_id,
                l1_code=meta["l1"],
                l2_code=meta["l2"],
                clause=meta["clause"],
                error_type=error_type,
                severity=round(min(1.0, weight), 3),
                confidence=round(min(1.0, 0.45 + weight * 0.4), 3),
                detail=detail,
                evidence=[evidence],
            )
            self._items[kp_id] = item
        else:
            item.evidence.append(evidence)
            item.severity = round(min(1.0, max(item.severity, weight)), 3)
            item.confidence = round(min(1.0, item.confidence + weight * 0.18), 3)
            if weight >= item.severity:
                item.detail = detail

    def build(self) -> list[KPAttribution]:
        items = list(self._items.values())
        items.sort(key=lambda item: (item.severity, item.confidence, len(item.evidence)), reverse=True)
        return items


def _add_ast_evidence(builder: _AttributionBuilder, std: dict[str, Any], stu: dict[str, Any]) -> None:
    expected_flags = [
        ("where", "has_where", "标准答案需要 WHERE 过滤，但学生 SQL 缺少过滤条件"),
        ("order-by", "has_order", "标准答案需要 ORDER BY 排序，但学生 SQL 缺少排序结构"),
        ("limit", "has_limit", "标准答案限制返回行数，但学生 SQL 缺少 LIMIT/OFFSET"),
        ("distinct", "has_distinct", "标准答案需要 DISTINCT 去重，但学生 SQL 缺少去重"),
        ("agg-count", "has_agg", "标准答案使用聚合函数，但学生 SQL 缺少聚合结构"),
        ("group-by", "has_group", "标准答案需要 GROUP BY 分组，但学生 SQL 缺少分组"),
        ("having", "has_having", "标准答案需要 HAVING 分组后筛选，但学生 SQL 缺少 HAVING"),
        ("subquery-scalar", "has_subquery", "标准答案使用子查询，但学生 SQL 未体现子查询结构"),
        ("cte", "has_cte", "标准答案使用 WITH/CTE，但学生 SQL 缺少 CTE"),
        ("union", "has_union", "标准答案使用 UNION 集合操作，但学生 SQL 缺少集合操作"),
        ("window-row-number", "has_window", "标准答案使用窗口函数，但学生 SQL 缺少窗口结构"),
        ("case", "has_case", "标准答案使用 CASE 条件表达式，但学生 SQL 缺少 CASE"),
    ]
    for kp_id, key, detail in expected_flags:
        if std.get(key) and not stu.get(key):
            builder.add(kp_id, "missing_clause", "E_AST", f"missing:{key}", detail, 0.9)

    if std["join_count"] > stu["join_count"]:
        builder.add("join-inner", "missing_clause", "E_AST", "missing:join", "标准答案需要更多 JOIN 结构，学生 SQL 连接数量不足", 0.9)
    if std["has_join_on"] and stu["join_count"] > 0 and not stu["has_join_on"]:
        builder.add("join-on", "missing_clause", "E_AST", "missing:join_on", "学生写了 JOIN，但缺少 ON 连接条件，可能形成笛卡尔积或错误连接", 0.95)

    for side, kp_id in [("LEFT", "join-left"), ("RIGHT", "join-right"), ("FULL", "join-full")]:
        if side in std["join_sides"] and side not in stu["join_sides"]:
            builder.add(kp_id, "wrong_join_type", "E_AST", f"join_type:{side}", f"标准答案需要 {side} JOIN，但学生没有使用对应外连接类型", 0.85)

    if std["projection_count"] and stu["projection_count"] and stu["projection_count"] < std["projection_count"]:
        builder.add("select-basic", "missing_partial", "E_AST", "projection_count", "学生 SELECT 输出列数量少于标准答案，可能漏选目标列或表达式", 0.7)


def _add_data_evidence(
    builder: _AttributionBuilder,
    judge: dict[str, Any],
    std: dict[str, Any],
    stu: dict[str, Any],
) -> None:
    message = str(judge.get("error_message") or "")
    if not message:
        return

    if "行数" in message:
        if std["has_where"]:
            builder.add("where", "data_mismatch", "E_data", "row_count", "结果行数不匹配，优先怀疑过滤条件边界、比较符或逻辑组合", 0.72)
        if std["has_distinct"]:
            builder.add("distinct", "data_mismatch", "E_data", "row_count_duplicate", "结果行数不匹配且标准答案需要 DISTINCT，可能漏掉去重", 0.7)
        if std["join_count"]:
            kp_id = "join-on" if stu["join_count"] and not stu["has_join_on"] else "join-inner"
            builder.add(kp_id, "data_mismatch", "E_data", "row_count_join", "结果行数不匹配且题目涉及 JOIN，可能连接条件或连接类型错误", 0.74)
        if std["has_group"]:
            builder.add("group-by", "data_mismatch", "E_data", "row_count_group", "结果行数不匹配且题目涉及分组，可能 GROUP BY 粒度错误", 0.72)
        if std["has_having"]:
            builder.add("having", "data_mismatch", "E_data", "row_count_having", "结果行数不匹配且题目涉及 HAVING，可能分组后筛选条件错误", 0.72)

    if "列结构" in message or "列数" in message or "缺少列" in message or "多余列" in message:
        builder.add("select-basic", "data_mismatch", "E_data", "column_shape", "结果列结构不匹配，可能 SELECT 投影列错误", 0.76)
        if judge.get("alias_enforced"):
            builder.add("alias", "data_mismatch", "E_data", "alias_required", "题目要求输出列名/别名，学生 SQL 的别名结构不一致", 0.72)

    ordered_compare = bool(judge.get("ordered_compare"))
    if ("ORDER BY" in message or (ordered_compare and "顺序" in message)) and (std["has_order"] or stu["has_order"] or ordered_compare):
        builder.add("order-by", "data_mismatch", "E_data", "row_order", "结果顺序不一致，可能 ORDER BY 字段或 ASC/DESC 方向错误", 0.78)

    if "结果数据不匹配" in message or "不一致" in message:
        if std["has_where"]:
            builder.add("where", "data_mismatch", "E_data", "value_mismatch_filter", "结果值不匹配，且题目包含 WHERE，可能过滤语义错误", 0.58)
        if std["has_agg"]:
            builder.add("agg-count", "data_mismatch", "E_data", "value_mismatch_agg", "结果值不匹配，且题目包含聚合，可能聚合函数或聚合对象错误", 0.58)
        if std["has_subquery"]:
            builder.add("subquery-scalar", "data_mismatch", "E_data", "value_mismatch_subquery", "结果值不匹配，且题目包含子查询，可能子查询条件或返回集合错误", 0.58)


def _add_mutation_evidence(
    builder: _AttributionBuilder,
    standard_ast: exp.Expression | None,
    student_ast: exp.Expression | None,
    mutation_detail: dict[str, Any] | None = None,
) -> None:
    if mutation_detail:
        for test in mutation_detail.get("tests") or []:
            kp_id = test.get("knowledge_point_id")
            if not kp_id:
                continue
            clause = test.get("clause") or kp_id
            action = test.get("action") or "mutation"
            if test.get("fixed_by_replacement"):
                builder.add(
                    kp_id,
                    "logical",
                    "E_MUT",
                    f"replacement_fixed:{clause}",
                    f"把学生 SQL 的 {clause} 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
                    0.88,
                )
            elif test.get("replacement_exec_ok") and test.get("replacement_equivalent") is False:
                builder.add(
                    kp_id,
                    "logical",
                    "E_MUT",
                    f"replacement_not_enough:{clause}",
                    f"替换 {clause} 后仍未通过动态沙盒，说明该子句相关但可能不是唯一错因。",
                    0.56,
                )
            if test.get("removed_student_clause_equivalent"):
                builder.add(
                    kp_id,
                    "complication",
                    "E_MUT",
                    f"remove_keeps_correct:{clause}",
                    f"移除学生 SQL 中的 {clause} 后结果仍正确，说明该结构可能是不必要复杂化。",
                    0.48,
                )

    clause_map = [
        ("where", exp.Where, "WHERE 子句与标准答案不一致，替换该子句是优先隔离方向"),
        ("group-by", exp.Group, "GROUP BY 子句与标准答案不一致，分组粒度是优先隔离方向"),
        ("having", exp.Having, "HAVING 子句与标准答案不一致，分组后筛选是优先隔离方向"),
        ("order-by", exp.Order, "ORDER BY 子句与标准答案不一致，排序字段或方向是优先隔离方向"),
        ("limit", exp.Limit, "LIMIT/OFFSET 与标准答案不一致，返回范围是优先隔离方向"),
    ]
    for kp_id, node_type, detail in clause_map:
        std_node = _first(standard_ast, node_type)
        stu_node = _first(student_ast, node_type)
        if std_node is not None and stu_node is not None and _node_sql(std_node) != _node_sql(stu_node):
            builder.add(kp_id, "clause_mismatch", "E_MUT", f"replace:{kp_id}", detail, 0.62)

    if _agg_names(standard_ast) and _agg_names(standard_ast) != _agg_names(student_ast):
        builder.add("agg-count", "clause_mismatch", "E_MUT", "replace:aggregation", "聚合函数集合与标准答案不一致，替换聚合表达式是优先隔离方向", 0.64)

    if _has_join_on(standard_ast) and _has_join_on(student_ast):
        std_join_sql = [_node_sql(node.args.get("on")) for node in _nodes(standard_ast, exp.Join)]
        stu_join_sql = [_node_sql(node.args.get("on")) for node in _nodes(student_ast, exp.Join)]
        if std_join_sql != stu_join_sql:
            builder.add("join-on", "clause_mismatch", "E_MUT", "replace:join_on", "JOIN ON 条件与标准答案不一致，连接谓词是优先隔离方向", 0.68)

    std_cases = [_node_sql(node) for node in _nodes(standard_ast, exp.Case)]
    stu_cases = [_node_sql(node) for node in _nodes(student_ast, exp.Case)]
    if std_cases and stu_cases and std_cases != stu_cases:
        builder.add("case", "clause_mismatch", "E_MUT", "replace:case", "CASE 条件表达式与标准答案不一致，优先检查 WHEN 条件、NULL 判断或 ELSE 分支", 0.68)


def evidence_weights_from_observation(
    *,
    student_sql: str,
    answer_sql: str,
    is_correct: bool,
    error_message: str | None = None,
    judge_detail: dict[str, Any] | None = None,
    question_context: dict[str, Any] | None = None,
    mutation_detail: dict[str, Any] | None = None,
) -> AttributionResult:
    """从观察证据中融合 KP 错因权重。"""
    standard_ast = _parse(answer_sql)
    student_ast = _parse(student_sql)
    std_features = _features(standard_ast)
    stu_features = _features(student_ast)
    judge_features = _judge_features(judge_detail, error_message)
    intended_kp = _kp_profile(role="intended", ast=standard_ast, features=std_features, question_context=question_context)
    observed_kp = _kp_profile(role="observed", ast=student_ast, features=stu_features)

    observation = {
        "E_AST": {
            "student_parse_ok": stu_features["parse_ok"],
            "standard_parse_ok": std_features["parse_ok"],
            "student_features": stu_features,
            "standard_features": std_features,
            "intended_kp": intended_kp,
            "observed_kp": observed_kp,
        },
        "E_data": judge_features,
        "E_MUT": {
            "enabled": bool(standard_ast and student_ast) and (not is_correct or bool(mutation_detail)),
            "mutation_tests": (mutation_detail or {}).get("tests") if mutation_detail else [],
            "mutation_summary": (mutation_detail or {}).get("summary") if mutation_detail else None,
        },
    }

    builder = _AttributionBuilder()
    misalignments: list[dict[str, Any]] = []
    if student_ast is None:
        builder.add("select-basic", "syntax_fatal", "E_AST", "parse_error", "学生 SQL 无法解析为合法查询语法，先归因到 SELECT 基础结构", 1.0)
    else:
        misalignments = _add_misalignment_evidence(builder, intended_kp, observed_kp, judge_features, is_correct)
        if not is_correct:
            _add_ast_evidence(builder, std_features, stu_features)
            _add_data_evidence(builder, judge_features, std_features, stu_features)
            _add_mutation_evidence(builder, standard_ast, student_ast, mutation_detail)

    attributions = builder.build()
    llm_input = {
        "question": (question_context or {}).get("q"),
        "question_context": question_context or {},
        "student_sql": student_sql,
        "answer_sql": answer_sql,
        "evidence": observation,
        "misalignment_comparison": misalignments,
        "candidate_kps": [item.to_dict() for item in attributions],
        "instructions": {
            "intended_kp_source": "question tags + standard SQL AST",
            "observed_kp_source": "student SQL AST",
            "decision_categories": ["Lacking", "Confusion", "Logical", "Generality", "Complication"],
        },
    }
    return AttributionResult(observation=observation, attributions=attributions, llm_arbitration_input=llm_input)


__all__ = [
    "AttributionResult",
    "EvidenceItem",
    "KPAttribution",
    "evidence_weights_from_observation",
]
