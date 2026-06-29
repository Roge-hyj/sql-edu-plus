"""
AST 结构差异分析（f_AST）

目标：
- 在不执行 SQL 的前提下，从“结构层面”解释学生 SQL 为什么错（或可能等价）。
- 输出一组结构化错误向量（ASTError 列表），供后续的 BKT 更新、控制策略计算、提示动作选择使用。

设计取舍：
- 仅靠 AST 规则会产生“等价写法”的假阳性（例如：LEFT JOIN + IS NULL vs NOT EXISTS）。
- 因此这里引入一次 *轻量* LLM 过滤：只对“结构差异清单”做真假错误判定，避免把风格差异当成能力缺陷。
- 如果 LLM 调用失败，会退回纯 AST 结果（偏保守，但可用）。

注意：
- 这个模块刻意不 import `core.ai_service`，避免与提示生成出现循环依赖。
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
    r"""
    【数据结构：结构化错误向量】
    记录学生 SQL 中确实缺少或错误的数学结构节点，作为控制系统中的观测值。
    数学定义：\mathbf{E}_t = [\min \sum_{i \in \text{path}_1} w(e_i), \dots, \min \sum_{i \in \text{path}_n} w(e_i)]^T
    """
    error_type: str          # 错误类型分类 (如: "missing_clause")
    clause: str              # 该错误的物理表征 (如: "GROUP BY", "JOIN")
    knowledge_point_id: str  # 对应数据库里用于 BKT 追踪的特定知识点 ID (如 "group-by")
    severity: float          # 权重/严重度 0.0-1.0，权重要大则下放到 A* 动作选择的优先级更高
    detail: str              # 通俗易懂的说明，用来输入给大模型当系统提示的根据

# 用于把知识点 id 映射成更直观的子句名（用于提示与动作选择）
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
    # 这里自己构建 OpenAI 客户端，避免与 core.ai_service 相互 import 导致循环导入。
    # 只用于“等价过滤”这一小步，不负责生成对学生的最终提示话术。
    return AsyncOpenAI(
        api_key=_settings.AI_API_KEY,
        base_url=_settings.AI_BASE_URL,
    )

def _as_nodes(res: Any) -> List[exp.Expression]:
    """把 extractor 的返回值统一成 Expression list，用于正确的 bool/len 判断。"""
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

# 【规则引擎】将知识点 ID 映射为对应的 SQLGlot 语法树节点的查询规则
# 这里定义了如何依靠树匹配找到学生到底缺了什么节点
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
    【推断题目知识覆盖库】
    解析题目的标准答案，并基于其本身的树节点逆向推导出该题考核了哪些知识。
    无需修改现存数据库手动录入知识关联表，极大方便教学题库的更新。

    返回值：
    - 若解析成功：返回该题“可能覆盖”的知识点 id 列表（与 CLAUSE_EXTRACTORS 对齐）
    - 若解析失败：退回 ["select-basic"]，保证下游 BKT 至少有一个维度可更新
    """
    try:
        ast = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.WARN)
    except Exception:
        return ["select-basic"]

    kps = []
    for kp_id, extractor in CLAUSE_EXTRACTORS.items():
        nodes = _as_nodes(extractor(ast))
        # 如果能在树中抽取到节点，说明含有此知识点考查
        if nodes:
            kps.append(kp_id)

    return kps if kps else ["select-basic"]

def _extract_ast_differences(student_ast: exp.Expression, answer_ast: exp.Expression) -> List[ASTError]:
    r"""
    【算法 1 步：粗粒度的 AST 节点集对比】
    提取 AST 语法树关键节点，对比标答树和学生树的子节点差距。
    此部相当于 Tree Edit Distance 的功能化落地，产出初始可能错位的节点集 \mathbf{E}'_t。

    重要：
    - 这里的差异是“结构级”的，不等同于“执行结果错”。
    - 输出只是候选差异，后续会经过 _llm_equivalence_filter 清洗降低误报。
    """
    differences = []

    for kp_id, extractor in CLAUSE_EXTRACTORS.items():
        # 分别抽取学生代码与标准代码里的特定维度语法节点
        student_nodes = _as_nodes(extractor(student_ast))
        answer_nodes = _as_nodes(extractor(answer_ast))
        clause_name = KP_CLAUSE_NAME.get(kp_id, kp_id.upper())

        # 1. 完全缺失类错误 (标答有的知识结构，学生完全没写)
        if answer_nodes and not student_nodes:
            differences.append(ASTError(
                error_type="missing_clause",
                clause=clause_name,
                knowledge_point_id=kp_id,
                severity=1.0, # 完全缺失惩罚权重高
                detail=f"预期使用了 {kp_id} 相关的结构，但学生代码完全缺失该节点"
            ))

        # 2. 局部数量不足/嵌套深度不足错误 (如：应连接3张表，只连接了2张)
        elif answer_nodes and student_nodes and len(student_nodes) < len(answer_nodes):
             differences.append(ASTError(
                error_type="missing_partial",
                clause=clause_name,
                knowledge_point_id=kp_id,
                severity=0.6, # 部分缺失权重偏中等
                detail=f"预期需要至少 {len(answer_nodes)} 处 {kp_id} 结构，学生代码数量不足"
            ))

    return differences

async def _llm_equivalence_filter(
    student_sql: str,
    answer_sql: str,
    raw_differences: List[ASTError]
) -> List[ASTError]:
    """
    【算法 2 步：LLM 等量语意推想兜底】
    纯编译器级别的 AST 会因为细微拼法(如 LEFT JOIN 与 RIGHT JOIN对调、IN vs EXISTS)
    报出极大差距，这对因材施教极不公平。
    我们会将第一步算出的结构级差异清单，再次投入一个快速廉价的 LLM 模型过滤逻辑。
    剔除假阳性的误判定。

    失败策略：
    - LLM 异常（网络/配额/解析失败）时直接返回 raw_differences，
      这样系统仍可继续运行，只是会更“保守”（多提示一些可能不存在的结构点）。
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
        "我会提供标准答案与学生的写法，以及一层简单抽象语法树比较器(AST)提出的若干个『结构变动预警』节点。\n"
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
            temperature=0.1,  # 采用低温确保推理稳定性
        )

        content = response.choices[0].message.content.strip()
        # 兼容大模型习惯包围的 Markdown 标识进行字符串裸取
        if content.startswith('```'):
            lines = content.split('\n')
            if len(lines) > 2:
                content = '\n'.join(lines[1:-1])

        # 解析 JSON 若被多套了一层 object 的自动平铺
        result_data = json.loads(content)
        if isinstance(result_data, dict) and len(result_data.keys()) == 1:
            result_data = list(result_data.values())[0]

        filtered_diffs = []
        for diff in raw_differences:
            # 默认保守设定：如果分析挂了，这个算作真实错误
            is_real = True
            for validation in result_data:
                if validation.get("kp_id") == diff.knowledge_point_id:
                    is_real = validation.get("is_real_error", True)
                    break

            # 仅仅收放 LLM 判实了的部分存入最终要返回的差异向量
            if is_real:
                filtered_diffs.append(diff)

        return filtered_diffs

    except Exception as e:
        print(f"LLM 语意判等失败，回退纯粹 AST 比对: {e}")
        traceback.print_exc()
        return raw_differences


async def compute_error_vector(
    student_sql: str,
    answer_sql: str,
    dialect: str = "mysql",
) -> List[ASTError]:
    r"""
    【总管式函数 $f_{\text{AST}}(S, A) \\to \\mathbf{E}_t$ 】

    1. 解析源和答案文本进入语法树格式。
    2. 计算获得原始报错集 \mathbf{E}'_t。
    3. LLM 清洗后抛出极高精度的确切错失节点向量 \mathbf{E}_t 提供给下层的贝叶斯追踪与提示组装使用。

    输入输出约定：
    - 返回 List[ASTError]；为空代表“结构上未发现高置信度缺陷”（不代表一定执行正确）
    - 若学生 SQL 语法严重崩溃：返回一个 syntax_fatal 的 ASTError，促使提示系统从基础引导
    """
    try:
        answer_ast = sqlglot.parse_one(answer_sql, dialect=dialect, error_level=ErrorLevel.WARN)
    except Exception as e:
        print(f"标答 SQL 解析出大问题了，请修正您的题目内容: {e}")
        return []

    try:
        student_ast = sqlglot.parse_one(student_sql, dialect=dialect, error_level=ErrorLevel.WARN)
    except Exception as e:
        # 学生写的代码崩溃严重到语法树都无法组装
        # 返还一个全盘报废类型的 ASTError 向量，直接要求教学系统从最基础从头教
        return [ASTError(
            error_type="syntax_fatal",
            clause="SYNTAX",
            knowledge_point_id="select-basic",
            severity=1.0,
            detail="基础 SQL 树语法严重崩盘，存在未闭合的括号或拼错的主关键字片段。"
        )]

    # 1. 精简的粗提取过程
    raw_differences = _extract_ast_differences(student_ast, answer_ast)

    # 2. 清洗过程
    verified_differences = await _llm_equivalence_filter(student_sql, answer_sql, raw_differences)

    return verified_differences