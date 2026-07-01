"""
AST Difference Analyzer (f_AST).

This module parses and analyzes SQL queries at a structural syntax level using sqlglot:
- Extracts structural representations (AST) from student and standard solution SQL queries.
- Compares AST nodes to detect missing clauses or structural mismatches.
- Resolves syntax equivalencies (e.g. JOIN variations, IN vs EXISTS) using a lightweight LLM filter.
- Outputs structured error vectors (ASTError instances) utilized in downstream BKT updates and hints generation.
"""

import traceback
import json
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Iterable

import sqlglot
from sqlglot import exp, ErrorLevel
from openai import AsyncOpenAI

from settings import get_settings

_settings = get_settings()

@dataclass
class ASTError:
    """
    Structured syntax error representation.

    Acts as an observation vector consumed by the BKT and pedagogical controller.
    Formulaic: E_t = [min sum w(e_i), ..., min sum w(e_n)]^T

    Attributes:
        error_type (str): Category classification of the error (e.g., "missing_clause").
        clause (str): Physical SQL clause identifier (e.g., "GROUP BY").
        knowledge_point_id (str): BKT taxonomy identifier (e.g., "group-by").
        severity (float): Error severity rating scaled [0.0, 1.0] for pedagogical prioritizing.
        detail (str): Plain-text feedback description.
    """
    error_type: str          # Error classification (e.g. "missing_clause")
    clause: str              # Affected SQL construct name (e.g. "GROUP BY", "JOIN")
    knowledge_point_id: str  # Matching taxonomy point ID in knowledge base
    severity: float          # Severity weight for tutoring system prioritization
    detail: str              # Explanatory text description


# Maps knowledge point IDs to user-friendly clause representations
KP_CLAUSE_NAME: Dict[str, str] = {
    "select-basic": "SELECT",
    "where": "WHERE",
    "order-by": "ORDER BY",
    "limit": "LIMIT",
    "distinct": "DISTINCT",
    "group-by": "GROUP BY",
    "having": "HAVING",
    "join-inner": "JOIN",
    "join-left": "LEFT JOIN",
    "join-right": "RIGHT JOIN",
    "join-full": "FULL JOIN",
    "agg-count": "AGGREGATION",
    "window-row-number": "WINDOW",
    "cte": "CTE",
    "subquery-scalar": "SUBQUERY",
    "case": "CASE",
    "union": "UNION",
}

def _get_client() -> AsyncOpenAI:
    """
    Creates an isolated OpenAI client specifically for the equivalence filter.

    Done to prevent circular import dependencies with core.ai_service.

    Returns:
        AsyncOpenAI: OpenAI client wrapper instance.
    """
    return AsyncOpenAI(
        api_key=_settings.AI_API_KEY,
        base_url=_settings.AI_BASE_URL,
    )

def _as_nodes(res: Any) -> List[exp.Expression]:
    """
    Coerces query extractor output into a standard list of sqlglot Expressions.

    Args:
        res (Any): Extractor output, which can be an expression, a list, or None.

    Returns:
        List[exp.Expression]: Standardized flat list of AST nodes.
    """
    if res is None:
        return []
    if isinstance(res, exp.Expression):
        return [res]
    if isinstance(res, (list, tuple, set)):
        return [x for x in res if isinstance(x, exp.Expression)]
    if isinstance(res, Iterable):
        out = []
        for x in res:
            if isinstance(x, exp.Expression):
                out.append(x)
        return out
    return []


# Mapping dictionary linking knowledge point IDs to AST query extractors (lambda search calls)
# Utilizes sqlglot's tree exploration methods (find, find_all) to scan nodes
CLAUSE_EXTRACTORS = {
    "select-basic":       lambda ast: ast.find(exp.Select),
    "where":              lambda ast: ast.find(exp.Where),
    "order-by":           lambda ast: ast.find(exp.Order),
    "limit":              lambda ast: ast.find(exp.Limit) or ast.find(exp.Offset),
    "distinct":           lambda ast: ast.find(exp.Distinct),
    "group-by":           lambda ast: ast.find(exp.Group),
    "having":             lambda ast: ast.find(exp.Having),
    "join-inner":         lambda ast: [j for j in ast.find_all(exp.Join) if (str(j.args.get("side") or "").upper() in ("", "INNER"))],
    "join-left":          lambda ast: [j for j in ast.find_all(exp.Join) if str(j.args.get("side") or "").upper() == "LEFT"],
    "join-right":         lambda ast: [j for j in ast.find_all(exp.Join) if str(j.args.get("side") or "").upper() == "RIGHT"],
    "join-full":          lambda ast: [j for j in ast.find_all(exp.Join) if str(j.args.get("side") or "").upper() == "FULL"],
    "agg-count":          lambda ast: ast.find_all(exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max),
    "window-row-number":  lambda ast: ast.find_all(exp.Window),
    "cte":                lambda ast: ast.find_all(exp.CTE),
    "subquery-scalar":    lambda ast: ast.find_all(exp.Subquery),
    "case":               lambda ast: ast.find_all(exp.Case),
    "union":              lambda ast: ast.find_all(exp.Union),
}

def infer_knowledge_points_from_sql(sql: str, dialect: str = "mysql") -> List[str]:
    """
    Parses a reference SQL query and infers which SQL knowledge points it covers.

    Allows the system to automatically tag question requirements dynamically.

    Args:
        sql (str): SQL statement to parse.
        dialect (str, optional): Target SQL dialect. Defaults to "mysql".

    Returns:
        List[str]: List of covered knowledge point IDs.
    """
    try:
        ast = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.WARN)
    except Exception:
        return ["select-basic"]

    kps = []
    for kp_id, extractor in CLAUSE_EXTRACTORS.items():
        nodes = _as_nodes(extractor(ast))
        if nodes:
            kps.append(kp_id)

    return kps if kps else ["select-basic"]

def _extract_ast_differences(student_ast: exp.Expression, answer_ast: exp.Expression) -> List[ASTError]:
    """
    Extracts structural discrepancies between the student and standard solution ASTs.

    Acts as a lightweight, rule-based Tree Edit Distance proxy to build a candidate list.

    Args:
        student_ast (exp.Expression): Student's parsed SQL syntax tree.
        answer_ast (exp.Expression): Reference solution's parsed SQL syntax tree.

    Returns:
        List[ASTError]: Extracted candidate AST errors.
    """
    differences = []

    for kp_id, extractor in CLAUSE_EXTRACTORS.items():
        student_nodes = _as_nodes(extractor(student_ast))
        answer_nodes = _as_nodes(extractor(answer_ast))
        clause_name = KP_CLAUSE_NAME.get(kp_id, kp_id.upper())

        # 1. Detect completely missing clauses
        if answer_nodes and not student_nodes:
            differences.append(ASTError(
                error_type="missing_clause",
                clause=clause_name,
                knowledge_point_id=kp_id,
                severity=1.0,
                detail=f"预期使用了 {kp_id} 相关的结构，但学生代码完全缺失该节点"
            ))

        # 2. Detect missing occurrences (e.g. joined tables count is less than expected)
        elif answer_nodes and student_nodes and len(student_nodes) < len(answer_nodes):
             differences.append(ASTError(
                error_type="missing_partial",
                clause=clause_name,
                knowledge_point_id=kp_id,
                severity=0.6,
                detail=f"预期需要至少 {len(answer_nodes)} 处 {kp_id} 结构，学生代码数量不足"
            ))

    return differences

async def _llm_equivalence_filter(
    student_sql: str,
    answer_sql: str,
    raw_differences: List[ASTError]
) -> List[ASTError]:
    """
    Filters out false-positive structural warnings (e.g. JOIN ordering) using an LLM.

    Ensures semantic equivalents (such as subquery replacements or join directions)
    are not falsely counted as capability flaws during scoring or BKT updates.

    Args:
        student_sql (str): Raw string of student's query.
        answer_sql (str): Raw string of standard solution.
        raw_differences (List[ASTError]): Initial candidates from AST comparisons.

    Returns:
        List[ASTError]: Filtered array of true structural error findings.
    """
    if not raw_differences:
        return []

    client = _get_client()
    model = (_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip()

    diff_json = [
        {"kp_id": d.knowledge_point_id, "detail": d.detail}
        for d in raw_differences
    ]

    system_prompt = (
        "你是一个极其理性的 SQL 语法等价逻辑判别裁判。\n"
        "我会提供标准答案与学生的写法，以及一层简单语法树比较器(AST)提出的若干个『结构变动预警』节点。\n"
        "【任务要求】：\n"
        "不要看错字这种小错。只要学生的写法能够起到和标答一样的业务目的（如用 LEFT JOIN 实现标答的 NOT EXISTS需求），"
        "或者只是顺序写反但不影响数据库实际提取过程（如表的关联先后不同但结果必同），你需要将其判定为 'false_positive' (不是真错误)。\n"
        "【返回格式限定】：\n"
        "请返回包含判定结果的纯 JSON 数组，必须具有这些 Key：\n"
        '- "kp_id": AST 报上来的变动点ID\n'
        '- "is_real_error": bool 值。它是否真的是错的（不等价）。若是伪差异请写 false\n'
        '- "reason": 中文解释理由'
    )

    user_prompt = (
        f"【标准答案 SQL】\n{answer_sql}\n\n"
        f"【学生试图写的错 SQL】\n{student_sql}\n\n"
        f"【AST 初步怀疑的差异缺陷点】\n{json.dumps(diff_json, ensure_ascii=False)}\n\n"
        "请判断以上缺陷点是真错误，还是因写法等价造成的假警报。"
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1,  # Low temperature for highly deterministic reasoning
        )

        content = response.choices[0].message.content.strip()
        # Parse output and remove possible markdown tags wrap
        if content.startswith('```'):
            lines = content.split('\n')
            if len(lines) > 2:
                content = '\n'.join(lines[1:-1])

        # Safely handle JSON root nesting variations
        result_data = json.loads(content)
        if isinstance(result_data, dict) and len(result_data.keys()) == 1:
            result_data = list(result_data.values())[0]

        filtered_diffs = []
        for diff in raw_differences:
            is_real = True
            for validation in result_data:
                if validation.get("kp_id") == diff.knowledge_point_id:
                    is_real = validation.get("is_real_error", True)
                    break

            if is_real:
                filtered_diffs.append(diff)

        return filtered_diffs

    except Exception as e:
        # Fallback to conservative AST reports if the AI service fails
        print(f"LLM 语意判等失败，回退纯粹 AST 比对: {e}")
        traceback.print_exc()
        return raw_differences


async def compute_error_vector(
    student_sql: str,
    answer_sql: str,
    dialect: str = "mysql",
) -> List[ASTError]:
    """
    Orchestrates the complete AST analysis workflow.

    1. Parses queries into structured syntax trees.
    2. Runs structural check rules to find raw discrepancies.
    3. Triggers the LLM validation to filter false alarms.

    Args:
        student_sql (str): SQL query submitted by the student.
        answer_sql (str): Standard reference solution SQL query.
        dialect (str, optional): Parsing dialect. Defaults to "mysql".

    Returns:
        List[ASTError]: List of verified structural discrepancies.
    """
    try:
        answer_ast = sqlglot.parse_one(answer_sql, dialect=dialect, error_level=ErrorLevel.WARN)
    except Exception as e:
        print(f"标答 SQL 解析出大问题了，请修正您的题目内容: {e}")
        return []

    try:
        student_ast = sqlglot.parse_one(student_sql, dialect=dialect, error_level=ErrorLevel.WARN)
    except Exception as e:
        # Fall back to a fatal syntax error type if parser fails completely
        return [ASTError(
            error_type="syntax_fatal",
            clause="SYNTAX",
            knowledge_point_id="select-basic",
            severity=1.0,
            detail="基础 SQL 树语法严重崩盘，存在未闭合的括号或拼错的主关键字片段。"
        )]

    raw_differences = _extract_ast_differences(student_ast, answer_ast)
    verified_differences = await _llm_equivalence_filter(student_sql, answer_sql, raw_differences)

    return verified_differences
