"""阶段 2 Diagnosis：把阶段 1 证据融合为知识点层级错因。

输入来自阶段 1 Observe 的证据包：
- E_AST：结构差分
- E_data：动态测试数据库上的结果差异
- E_MUT：子句替换/隔离方向

输出是可解释的最终知识点归因。默认确定性融合保证离线复现；需要在线诊断时，
可调用 diagnose_record_with_llm，把双向 KP 比对和 E_AST/E_data/E_MUT 证据交给 LLM
在候选知识点范围内做最终归因。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import json
from typing import Any


SOURCE_RELIABILITY = {
    "E_AST": 1.0,
    "E_MUT": 0.9,
    "E_data": 0.78,
}

L1_LABELS = {
    "KP_BASIC": "基础查询",
    "KP_FILTER": "筛选逻辑",
    "KP_ORDER": "排序与返回范围",
    "KP_AGG": "聚合与分组",
    "KP_JOIN": "连接查询",
    "KP_SUBQUERY": "子查询",
    "KP_ADVANCED": "高级 SQL 结构",
    "KP_FUNC": "函数与表达式",
}

L2_LABELS = {
    "PROJ_COL": "SELECT 投影列",
    "PROJ_EXPR": "SELECT 表达式",
    "ALIAS_COL": "列/表别名",
    "DISTINCT_SET": "DISTINCT 去重",
    "LIMIT_OFF": "LIMIT/TOP 返回范围",
    "SORT_ASC": "升序排序",
    "SORT_DESC": "降序排序",
    "COMP_VAL": "比较条件",
    "COMP_NULL": "NULL 判断",
    "LOGIC_AND_OR": "AND/OR 逻辑组合",
    "LOGIC_NOT": "NOT/反向条件",
    "RANGE_BET": "范围条件",
    "LIKE_STR": "LIKE 字符串匹配",
    "SET_IN": "IN/NOT IN 集合判断",
    "AGG_BASIC": "聚合函数",
    "GB_SIMPLE": "GROUP BY 分组",
    "HV_SIMPLE": "HAVING 分组后筛选",
    "JOIN_INNER": "INNER JOIN",
    "JOIN_LEFT": "LEFT JOIN",
    "JOIN_RIGHT": "RIGHT JOIN",
    "JOIN_FULL": "FULL JOIN",
    "JOIN_CROSS": "CROSS JOIN",
    "JOIN_ON": "JOIN ON 连接条件",
    "SUB_TABLE": "派生表/表子查询",
    "SUB_IN_ALL_ANY": "IN/ALL/ANY 子查询",
    "SUB_EXISTS": "EXISTS/NOT EXISTS 子查询",
    "SET_UNION": "UNION 集合操作",
    "SET_EXCEPT": "EXCEPT 差集",
    "CTE_SIMPLE": "普通 CTE",
    "CTE_RECURSIVE": "递归 CTE",
    "WIN_OVER": "窗口函数 OVER",
    "WIN_RANK": "窗口排名函数",
    "WIN_FRAME": "窗口框架/ROLLUP 类分层聚合",
    "CASE_SEARCH": "CASE 条件表达式",
    "NULL_COAL": "COALESCE/空值合并",
}

KP_TO_L1 = {
    "select-basic": "KP_BASIC",
    "where": "KP_FILTER",
    "order-by": "KP_ORDER",
    "limit": "KP_ORDER",
    "limit-offset": "KP_ORDER",
    "distinct": "KP_BASIC",
    "alias": "KP_BASIC",
    "agg-count": "KP_AGG",
    "group-by": "KP_AGG",
    "having": "KP_AGG",
    "join-inner": "KP_JOIN",
    "join-on": "KP_JOIN",
    "join-left": "KP_JOIN",
    "join-right": "KP_JOIN",
    "join-full": "KP_JOIN",
    "subquery-scalar": "KP_SUBQUERY",
    "subquery-in": "KP_SUBQUERY",
    "subquery-exists": "KP_SUBQUERY",
    "cte": "KP_ADVANCED",
    "union": "KP_ADVANCED",
    "window-row-number": "KP_ADVANCED",
    "case": "KP_FILTER",
}

L2_TO_SYNTHETIC_KP = {
    "DISTINCT_SET": ("distinct", "KP_BASIC", "DISTINCT"),
    "COMP_VAL": ("where", "KP_FILTER", "WHERE"),
    "COMP_NULL": ("where", "KP_FILTER", "WHERE"),
    "LOGIC_AND_OR": ("where", "KP_FILTER", "WHERE"),
    "LOGIC_NOT": ("where", "KP_FILTER", "WHERE"),
    "LIKE_STR": ("where", "KP_FILTER", "WHERE"),
    "SET_IN": ("subquery-in", "KP_SUBQUERY", "IN SUBQUERY"),
    "AGG_BASIC": ("agg-count", "KP_AGG", "AGGREGATION"),
    "GB_SIMPLE": ("group-by", "KP_AGG", "GROUP BY"),
    "HV_SIMPLE": ("having", "KP_AGG", "HAVING"),
    "JOIN_INNER": ("join-inner", "KP_JOIN", "JOIN"),
    "JOIN_LEFT": ("join-left", "KP_JOIN", "LEFT JOIN"),
    "JOIN_RIGHT": ("join-right", "KP_JOIN", "RIGHT JOIN"),
    "JOIN_FULL": ("join-full", "KP_JOIN", "FULL JOIN"),
    "JOIN_CROSS": ("join-inner", "KP_JOIN", "CROSS JOIN"),
    "JOIN_ON": ("join-on", "KP_JOIN", "JOIN ON"),
    "SUB_TABLE": ("subquery-scalar", "KP_SUBQUERY", "SUBQUERY"),
    "SUB_IN_ALL_ANY": ("subquery-in", "KP_SUBQUERY", "IN SUBQUERY"),
    "SUB_EXISTS": ("subquery-exists", "KP_SUBQUERY", "EXISTS"),
    "SET_UNION": ("union", "KP_ADVANCED", "UNION"),
    "SET_EXCEPT": ("union", "KP_ADVANCED", "EXCEPT"),
    "CTE_SIMPLE": ("cte", "KP_ADVANCED", "WITH"),
    "CTE_RECURSIVE": ("cte", "KP_ADVANCED", "WITH RECURSIVE"),
    "WIN_OVER": ("window-row-number", "KP_ADVANCED", "WINDOW"),
    "WIN_RANK": ("window-row-number", "KP_ADVANCED", "WINDOW"),
    "WIN_FRAME": ("window-row-number", "KP_ADVANCED", "WINDOW"),
    "CASE_SEARCH": ("case", "KP_FILTER", "CASE"),
    "NULL_COAL": ("case", "KP_FILTER", "CASE"),
}

PEDAGOGICAL_PRIORITY = {
    "CTE_RECURSIVE": 1.0,
    "SET_EXCEPT": 0.96,
    "SUB_EXISTS": 0.92,
    "SET_UNION": 0.9,
    "WIN_RANK": 0.88,
    "WIN_OVER": 0.86,
    "SUB_IN_ALL_ANY": 0.84,
    "SUB_TABLE": 0.8,
    "JOIN_FULL": 0.76,
    "JOIN_LEFT": 0.74,
    "JOIN_CROSS": 0.72,
    "JOIN_INNER": 0.7,
    "JOIN_ON": 0.68,
    "HV_SIMPLE": 0.66,
    "GB_SIMPLE": 0.62,
    "AGG_BASIC": 0.58,
    "SET_IN": 0.54,
    "DISTINCT_SET": 0.5,
    "LIKE_STR": 0.42,
    "COMP_NULL": 0.4,
    "LOGIC_NOT": 0.38,
    "LOGIC_AND_OR": 0.34,
    "COMP_VAL": 0.28,
    "PROJ_EXPR": 0.18,
    "PROJ_COL": 0.12,
    "ALIAS_COL": 0.08,
}

PLACEHOLDER_SIMPLE_KPS = {"select-basic", "alias"}
PLACEHOLDER_CORE_THRESHOLD = 0.68


@dataclass
class DiagnosisItem:
    """一个最终知识点级错因。"""

    rank: int
    knowledge_point_id: str
    l1_code: str
    l1_label: str
    l2_code: str
    l2_label: str
    clause: str
    error_type: str
    score: float
    confidence: float
    severity: float
    decision: str
    rationale: str
    evidence_chain: list[dict[str, Any]] = field(default_factory=list)
    source_scores: dict[str, float] = field(default_factory=dict)
    matches_question_target: bool = False
    misalignment_type: str | None = None
    intended_observed_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _upper_sql(sql: str | None) -> str:
    return f" {sql or ''} ".upper()


def _has(sql: str, token: str) -> bool:
    return token in _upper_sql(sql)


def _refine_l2(candidate: dict[str, Any], record: dict[str, Any]) -> str:
    """把粗 KP 映射到题库使用的 L2 代码。

    阶段 1 会先定位到 where/group-by/join-on 这样的粗知识点。阶段 2 再结合
    标答 SQL、学生 SQL 和题目目标知识点，细化到 LIKE_STR、LOGIC_NOT、JOIN_ON 等。
    """

    kp_id = candidate.get("knowledge_point_id") or ""
    current_l2 = candidate.get("l2_code") or ""
    question_l2 = set((record.get("question_context") or {}).get("l2") or [])
    standard_sql = record.get("standard_sql") or ""
    student_sql = record.get("student_sql") or ""

    if kp_id == "where":
        standard = _upper_sql(standard_sql)
        student = _upper_sql(student_sql)
        refinements = [
            ("LIKE_STR", " LIKE "),
            ("SET_IN", " IN "),
            ("RANGE_BET", " BETWEEN "),
            ("COMP_NULL", " IS NULL"),
            ("COMP_NULL", " IS NOT NULL"),
        ]
        for l2, token in refinements:
            if l2 in question_l2 and (token in standard or token in student):
                return l2
        if "LOGIC_NOT" in question_l2 and (
            " NOT " in standard
            or " NOT " in student
            or "<>" in standard
            or "<>" in student
            or "!=" in standard
            or "!=" in student
        ):
            return "LOGIC_NOT"
        if "LOGIC_AND_OR" in question_l2 and (
            " AND " in standard
            or " OR " in standard
            or " AND " in student
            or " OR " in student
        ):
            return "LOGIC_AND_OR"
        return "COMP_VAL" if "COMP_VAL" in question_l2 else current_l2

    if kp_id == "order-by":
        if " DESC" in _upper_sql(standard_sql) and "SORT_DESC" in question_l2:
            return "SORT_DESC"
        return "SORT_ASC" if "SORT_ASC" in question_l2 else current_l2

    if kp_id in {"limit", "limit-offset"}:
        return "LIMIT_OFF"

    if kp_id == "cte":
        return "CTE_RECURSIVE" if _has(standard_sql, "WITH RECURSIVE") else "CTE_SIMPLE"

    if kp_id == "union":
        if _has(standard_sql, " EXCEPT ") and "SET_EXCEPT" in question_l2:
            return "SET_EXCEPT"
        return "SET_UNION"

    if kp_id == "window-row-number":
        if " RANK(" in _upper_sql(standard_sql) and "WIN_RANK" in question_l2:
            return "WIN_RANK"
        if "ROLLUP" in _upper_sql(standard_sql) and "WIN_FRAME" in question_l2:
            return "WIN_FRAME"
        return "WIN_OVER"

    if kp_id == "case":
        standard = _upper_sql(standard_sql)
        student = _upper_sql(student_sql)
        if "COMP_NULL" in question_l2 and (" NULL" in standard or " NULL" in student):
            return "COMP_NULL"
        if "LOGIC_NOT" in question_l2 and (" NOT " in standard or " NOT " in student or "<>" in student or "!=" in student):
            return "LOGIC_NOT"
        return "CASE_SEARCH"

    if kp_id == "subquery-in":
        return "SUB_IN_ALL_ANY"
    if kp_id == "subquery-exists":
        return "SUB_EXISTS"
    if kp_id == "subquery-scalar":
        return "SUB_TABLE" if "SUB_TABLE" in question_l2 else "SUB_IN_ALL_ANY" if "SUB_IN_ALL_ANY" in question_l2 else current_l2

    return current_l2


def _pedagogical_priority(l2_code: str) -> float:
    return PEDAGOGICAL_PRIORITY.get(l2_code, 0.0)


def _question_core_l2(record: dict[str, Any]) -> str | None:
    question_l2 = (record.get("question_context") or {}).get("l2") or []
    candidates = [l2 for l2 in question_l2 if l2 in L2_TO_SYNTHETIC_KP]
    if not candidates:
        return None
    return max(candidates, key=_pedagogical_priority)


def _is_placeholder_attempt(record: dict[str, Any]) -> bool:
    """Detect low-engagement placeholder SQL before fine-grained attribution.

    A single-table projection can miss many low-level clauses simply because the
    learner abandoned the hard structure. In that case the teaching diagnosis
    should target the highest core objective of the question, not the first
    missing primitive such as WHERE or DISTINCT.
    """

    ast = (record.get("phase1_observation") or {}).get("E_AST") or {}
    intended = ast.get("intended_kp") or {}
    observed = ast.get("observed_kp") or {}
    std_features = ast.get("standard_features") or {}
    stu_features = ast.get("student_features") or {}
    target_kps = set(intended.get("structural_kps") or [])
    observed_kps = set(observed.get("structural_kps") or [])
    if not target_kps or not observed_kps:
        return False

    core_l2 = _question_core_l2(record)
    if not core_l2 or _pedagogical_priority(core_l2) < PLACEHOLDER_CORE_THRESHOLD:
        return False

    question_complex_target = _pedagogical_priority(core_l2) >= PLACEHOLDER_CORE_THRESHOLD
    complex_target = bool(
        std_features.get("join_count", 0)
        or std_features.get("has_subquery")
        or std_features.get("has_cte")
        or std_features.get("has_union")
        or std_features.get("has_window")
        or std_features.get("has_group")
        or std_features.get("has_having")
        or question_complex_target
    )
    student_simple = (
        observed_kps <= PLACEHOLDER_SIMPLE_KPS
        and not stu_features.get("has_where")
        and not stu_features.get("has_agg")
        and not stu_features.get("has_group")
        and not stu_features.get("has_having")
        and not stu_features.get("has_subquery")
        and not stu_features.get("has_cte")
        and not stu_features.get("has_union")
        and not stu_features.get("has_window")
        and not stu_features.get("join_count", 0)
    )
    overlap = len(target_kps & observed_kps) / max(1, len(target_kps))
    projection_only = (stu_features.get("projection_count") or 0) <= 2
    return bool(complex_target and student_simple and projection_only and overlap <= 0.34)


def _placeholder_candidate(record: dict[str, Any]) -> dict[str, Any] | None:
    core_l2 = _question_core_l2(record)
    if not core_l2:
        return None
    kp_id, l1_code, clause = L2_TO_SYNTHETIC_KP[core_l2]
    priority = _pedagogical_priority(core_l2)
    return {
        "knowledge_point_id": kp_id,
        "l1_code": l1_code,
        "l2_code": core_l2,
        "clause": clause,
        "error_type": "abandoned_attempt",
        "severity": round(0.9 + min(0.09, priority * 0.09), 3),
        "confidence": round(0.86 + min(0.12, priority * 0.12), 3),
        "detail": (
            "学生提交与标答结构重叠极低，表现为低参与度/占位符提交；"
            f"主诊断收敛到题目核心高阶目标 {core_l2}。"
        ),
        "evidence": [
            {
                "source": "E_AST",
                "signal": "placeholder_low_engagement",
                "detail": (
                    "学生 SQL 只有简单投影，缺少题目核心结构；"
                    "跳过浅表缺失项，优先诊断题目最高阶目标知识点。"
                ),
                "weight": 0.98,
            }
        ],
    }


def _source_scores(candidate: dict[str, Any]) -> dict[str, float]:
    scores: Counter[str] = Counter()
    for evidence in candidate.get("evidence") or []:
        source = evidence.get("source") or "unknown"
        raw_weight = float(evidence.get("weight") or 0.0)
        scores[source] += raw_weight * SOURCE_RELIABILITY.get(source, 0.6)
    return {source: round(value, 3) for source, value in scores.items()}


def _candidate_misalignment_type(candidate: dict[str, Any], record: dict[str, Any]) -> str:
    explicit = str(candidate.get("error_type") or "").lower()
    signals = " ".join(str(ev.get("signal") or "") for ev in candidate.get("evidence") or [])
    if (
        "clause_confusion" in signals
        or "illegal_location" in signals
        or "join_type" in signals
        or "join_without_on" in signals
    ):
        return "Confusion"
    if explicit == "abandoned_attempt":
        return "Lacking"
    mapping = {
        "lacking": "Lacking",
        "missing_clause": "Lacking",
        "missing_partial": "Lacking",
        "confusion": "Confusion",
        "wrong_join_type": "Confusion",
        "logical": "Logical",
        "clause_mismatch": "Logical",
        "data_mismatch": "Logical",
        "generality": "Generality",
        "complication": "Complication",
    }
    if explicit in mapping:
        return mapping[explicit]

    kp_id = candidate.get("knowledge_point_id")
    for item in (record.get("llm_arbitration_input") or {}).get("misalignment_comparison") or []:
        if item.get("knowledge_point_id") == kp_id and item.get("category"):
            return item["category"]

    if "target_missing" in signals or "missing" in signals:
        return "Lacking"
    if "confusion" in signals or "illegal_location" in signals or "join_without_on" in signals:
        return "Confusion"
    if "counterexample" in signals:
        return "Generality"
    if "complexity" in signals or "remove_keeps_correct" in signals:
        return "Complication"
    return "Logical"


def _diagnosis_error_type(candidate: dict[str, Any], record: dict[str, Any]) -> str:
    explicit = str(candidate.get("error_type") or "")
    if explicit == "abandoned_attempt":
        return explicit
    misalignment_type = _candidate_misalignment_type(candidate, record)
    if misalignment_type == "Confusion":
        return "confusion"
    if misalignment_type == "Logical":
        return "logical"
    return explicit or misalignment_type.lower()


def _intended_observed_summary(record: dict[str, Any], candidate: dict[str, Any], refined_l2: str) -> dict[str, Any]:
    ast = record.get("phase1_observation", {}).get("E_AST", {})
    intended = ast.get("intended_kp") or {}
    observed = ast.get("observed_kp") or {}
    kp_id = candidate.get("knowledge_point_id")
    return {
        "target_l1": intended.get("question_l1"),
        "target_l2": intended.get("question_l2"),
        "candidate_kp": kp_id,
        "candidate_l2": refined_l2,
        "target_has_kp": kp_id in set(intended.get("structural_kps") or []),
        "student_has_kp": kp_id in set(observed.get("structural_kps") or []),
        "target_comparison_locations": intended.get("comparison_locations") or {},
        "student_comparison_locations": observed.get("comparison_locations") or {},
        "student_illegal_aggregate_locations": observed.get("illegal_aggregate_locations") or [],
        "student_complexity_kps": observed.get("complexity_kps") or [],
    }


def _score_candidate(candidate: dict[str, Any], record: dict[str, Any], refined_l2: str) -> tuple[float, dict[str, float], bool]:
    source_scores = _source_scores(candidate)
    source_strength = min(1.0, sum(source_scores.values()) / 2.0)
    severity = float(candidate.get("severity") or 0.0)
    confidence = float(candidate.get("confidence") or 0.0)
    question_context = record.get("question_context") or {}
    question_l1 = question_context.get("l1")
    question_l2 = set(question_context.get("l2") or [])
    matches_target = bool(refined_l2 in question_l2 or candidate.get("l1_code") == question_l1)

    score = 0.42 * confidence + 0.33 * severity + 0.25 * source_strength
    if "E_MUT" in source_scores:
        score += 0.1
    if "E_AST" in source_scores and any("missing" in str((ev.get("signal") or "")) for ev in candidate.get("evidence") or []):
        score += 0.08
    if refined_l2 in question_l2:
        score += 0.12
    elif candidate.get("l1_code") == question_l1:
        score += 0.06
    else:
        score -= 0.06

    data = record.get("phase1_observation", {}).get("E_data", {})
    if data.get("row_count_match") is False and "E_data" in source_scores:
        score += 0.04
    if data.get("columns_match") is False and refined_l2 in {"PROJ_COL", "PROJ_EXPR", "ALIAS_COL"}:
        score += 0.05

    score += _pedagogical_priority(refined_l2) * 0.08
    if candidate.get("error_type") == "abandoned_attempt":
        score += 0.18

    misalignment_type = _candidate_misalignment_type(candidate, record)
    if misalignment_type in {"Lacking", "Confusion", "Logical"}:
        score += 0.04
    if misalignment_type == "Confusion":
        score += 0.06
    elif misalignment_type == "Generality":
        score += 0.08
    elif misalignment_type == "Complication":
        score -= 0.08

    return round(max(0.0, min(1.0, score)), 3), source_scores, matches_target


def _rationale(item: DiagnosisItem) -> str:
    if item.evidence_chain:
        strongest = item.evidence_chain[0]
        return (
            f"最终归因到 {item.l2_code}（{item.l2_label}）："
            f"最高权重证据来自 {strongest.get('source')} 的 {strongest.get('signal')}，"
            f"并与题目目标知识点{'一致' if item.matches_question_target else '部分相关'}。"
        )
    return f"最终归因到 {item.l2_code}（{item.l2_label}）。"


def diagnose_record(record: dict[str, Any], *, max_items: int = 3) -> dict[str, Any]:
    """对一条阶段 1 记录输出最终知识点级诊断。"""

    candidates = list(record.get("phase2_candidate_attributions") or [])
    if record.get("sandbox_status") == "Correct":
        return {
            "persona": record.get("persona"),
            "q_id": record.get("q_id"),
            "diagnosis_status": "Correct",
            "primary_diagnosis": None,
            "final_attributions": [],
            "rejected_candidates": [],
        }
    placeholder = _is_placeholder_attempt(record)
    if placeholder:
        synthetic = _placeholder_candidate(record)
        if synthetic:
            candidates.insert(0, synthetic)

    if not candidates:
        return {
            "persona": record.get("persona"),
            "q_id": record.get("q_id"),
            "diagnosis_status": "Incorrect",
            "primary_diagnosis": None,
            "final_attributions": [],
            "rejected_candidates": [],
            "undetermined_reason": "阶段 1 未产生候选 KP 归因，保留为错误但不强行归因。",
        }

    scored: list[tuple[float, dict[str, float], bool, dict[str, Any], str]] = []
    for candidate in candidates:
        refined_l2 = _refine_l2(candidate, record)
        score, source_scores, matches_target = _score_candidate(candidate, record, refined_l2)
        scored.append((score, source_scores, matches_target, candidate, refined_l2))

    scored.sort(
        key=lambda item: (
            item[0],
            _pedagogical_priority(item[4]),
            item[3].get("severity") or 0,
            item[3].get("confidence") or 0,
        ),
        reverse=True,
    )
    selected = [item for item in scored if item[0] >= 0.55][:max_items]
    if not selected and scored:
        selected = [scored[0]]

    final_items: list[DiagnosisItem] = []
    for rank, (score, source_scores, matches_target, candidate, refined_l2) in enumerate(selected, start=1):
        l1_code = KP_TO_L1.get(candidate.get("knowledge_point_id"), candidate.get("l1_code") or "")
        evidence_chain = sorted(candidate.get("evidence") or [], key=lambda ev: float(ev.get("weight") or 0), reverse=True)
        diagnosis_error_type = _diagnosis_error_type(candidate, record)
        misalignment_type = _candidate_misalignment_type(candidate, record)
        item = DiagnosisItem(
            rank=rank,
            knowledge_point_id=candidate.get("knowledge_point_id") or "",
            l1_code=l1_code,
            l1_label=L1_LABELS.get(l1_code, l1_code),
            l2_code=refined_l2,
            l2_label=L2_LABELS.get(refined_l2, refined_l2),
            clause=candidate.get("clause") or "",
            error_type=diagnosis_error_type,
            score=score,
            confidence=round(float(candidate.get("confidence") or score), 3),
            severity=round(float(candidate.get("severity") or score), 3),
            decision="primary" if rank == 1 else "secondary",
            rationale="",
            evidence_chain=evidence_chain,
            source_scores=source_scores,
            matches_question_target=matches_target,
            misalignment_type=misalignment_type,
            intended_observed_summary=_intended_observed_summary(record, candidate, refined_l2),
        )
        item.rationale = _rationale(item)
        final_items.append(item)

    selected_ids = {id(item[3]) for item in selected}
    rejected = []
    for score, source_scores, matches_target, candidate, refined_l2 in scored:
        if id(candidate) in selected_ids:
            continue
        rejected.append({
            "knowledge_point_id": candidate.get("knowledge_point_id"),
            "l2_code": refined_l2,
            "score": score,
            "source_scores": source_scores,
            "reason": "分数低于最终归因阈值，作为候选保留但不进入主诊断。",
            "matches_question_target": matches_target,
            "misalignment_type": _candidate_misalignment_type(candidate, record),
        })

    return {
        "persona": record.get("persona"),
        "q_id": record.get("q_id"),
        "diagnosis_status": "Incorrect",
        "primary_diagnosis": final_items[0].to_dict() if final_items else None,
        "final_attributions": [item.to_dict() for item in final_items],
        "rejected_candidates": rejected,
        "placeholder_attempt": placeholder,
    }


def diagnose_records(records: list[dict[str, Any]], *, max_items: int = 3) -> list[dict[str, Any]]:
    return [diagnose_record(record, max_items=max_items) for record in records]


def _extract_json_object(text: str) -> dict[str, Any] | None:
    clean = (text or "").strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if len(lines) >= 3:
            clean = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(clean)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = clean.find("{")
    end = clean.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(clean[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _build_llm_messages(record: dict[str, Any], deterministic: dict[str, Any]) -> list[dict[str, str]]:
    arbitration = record.get("llm_arbitration_input") or {}
    compact_payload = {
        "question_context": record.get("question_context") or arbitration.get("question_context") or {},
        "standard_sql": record.get("standard_sql"),
        "student_sql": record.get("student_sql"),
        "phase1_observation": record.get("phase1_observation") or arbitration.get("evidence") or {},
        "misalignment_comparison": arbitration.get("misalignment_comparison") or [],
        "candidate_kps": record.get("phase2_candidate_attributions") or arbitration.get("candidate_kps") or [],
        "deterministic_baseline": deterministic,
    }
    system_prompt = (
        "你是 SQL 教学诊断中的阶段2归因器。你只能基于输入证据，在候选知识点范围内做最终归因。"
        "必须做双向比对：Intended KP 来自题目标签和标答 AST，Observed KP 来自学生 AST。"
        "错误类别只能是 Lacking、Confusion、Logical、Generality、Complication。"
        "不要只看学生缺了什么，还要判断学生是否写了相似但错位的结构、边界/操作符/布尔逻辑错误、"
        "动态反例暴露的泛化错误，或结果正确但不必要复杂化。"
        "返回纯 JSON，不要 Markdown。"
    )
    user_prompt = (
        "请输出如下 JSON："
        '{"diagnosis_status":"Correct|Incorrect","primary_diagnosis":object|null,'
        '"final_attributions":[{"knowledge_point_id":str,"l1_code":str,"l2_code":str,'
        '"misalignment_type":str,"confidence":number,"severity":number,"rationale":str}],'
        '"llm_notes":str}。输入证据如下：\n'
        f"{json.dumps(compact_payload, ensure_ascii=False)}"
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]


async def diagnose_record_with_llm(record: dict[str, Any], *, max_items: int = 3) -> dict[str, Any]:
    """调用 LLM 完成阶段 2 知识点归因，失败时回退确定性诊断。"""

    deterministic = diagnose_record(record, max_items=max_items)
    if deterministic.get("diagnosis_status") == "Correct":
        return deterministic

    try:
        from openai import AsyncOpenAI
        from settings import get_settings

        settings = get_settings()
        client = AsyncOpenAI(api_key=settings.AI_API_KEY, base_url=settings.AI_BASE_URL)
        response = await client.chat.completions.create(
            model=(settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip(),
            messages=_build_llm_messages(record, deterministic),
            temperature=0.1,
        )
        content = response.choices[0].message.content or ""
        parsed = _extract_json_object(content)
        if not parsed:
            return {**deterministic, "llm_status": "fallback_parse_failed"}
        if parsed.get("diagnosis_status") not in {"Correct", "Incorrect"}:
            parsed["diagnosis_status"] = deterministic.get("diagnosis_status")
        parsed["deterministic_baseline"] = deterministic
        parsed["llm_status"] = "ok"
        return parsed
    except Exception as exc:
        return {**deterministic, "llm_status": "fallback_error", "llm_error": str(exc)}


async def diagnose_records_with_llm(records: list[dict[str, Any]], *, max_items: int = 3) -> list[dict[str, Any]]:
    out = []
    for record in records:
        out.append(await diagnose_record_with_llm(record, max_items=max_items))
    return out


__all__ = [
    "DiagnosisItem",
    "diagnose_record",
    "diagnose_records",
    "diagnose_record_with_llm",
    "diagnose_records_with_llm",
]
