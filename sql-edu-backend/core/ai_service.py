"""
AI 提示与多轮对话服务（LLM Service）

职责：
- `get_sql_hint`: 生成判题后的第一轮教学提示（自然语言）
- `chat_with_teacher`: 多轮对话继续追问与引导（自然语言）

设计原则：
- 以“最小改动”引导学生修正，而不是直接给出完整答案 SQL
- 支架等级 hint_level 仅用于内部策略，不向学生显式展示
- 若上游提供 hint_actions / lambda_t，则采用更精细的动作组合注入（闭环控制）
"""

import json
import traceback
from typing import Optional, List

# 移除 LangChain 导入，改用原生 OpenAI
from openai import AsyncOpenAI

from settings import get_settings
from schemas.agent import SQLCheckResultSchema
from core.scaffolding import get_scaffolding_instruction
from core.ast_analyzer import ASTError
from core.hint_actions import HintAction

_settings = get_settings()

def _build_system_prompt(
    hint_level: int = 1,
    language: str = "zh-CN",
    hint_actions: Optional[List[HintAction]] = None,
    lambda_t: Optional[float] = None
) -> str:
    """构建 System 提示词

    :param hint_level: 支架等级
    :param language: 回答语言，可选值：zh-CN(简体中文)、en(英文)、zh-TW(繁体中文)
    """
    if hint_actions:
        # 兜底：如果调用方传了 hint_actions 但没传 lambda_t，避免格式化时报错
        if lambda_t is None:
            lambda_t = 0.5

        action_instructions = []
        for action in hint_actions:
            action_instructions.append(
                f"- 针对涉及到「{action.target_knowledge_point}」的结构性差异点：\n"
                f"  目标节点：{action.clause_name}\n  策略指示：{action.prompt_fragment}"
            )

        # 覆写传统的 rule-based scaffolding
        scaffolding_instruction = (
            f"【算法当前监控到的学生严厉指数 $\\lambda_t$ 为：{lambda_t:.2f}。】\n"
            f"(其中1.0代表精力充沛且学习吸收高，应给予最大挑战。0.0代表学生极端疲劳且挫折感高，需要详细抚慰与保姆级引导。因此请对应调节你的冷暖情绪和措辞。)\n\n"
            "请严格针对探测出的 AST 逻辑问题，按以下组合策略分别进行针对性提示闭环构建：\n"
            + "\n".join(action_instructions)
        )
    else:
        # 向下兼容传统的 1, 2, 3 级报错统计
        scaffolding_instruction = get_scaffolding_instruction(hint_level)

    # 根据语言设置回答语言指令
    language_instructions = {
        "zh-CN": "使用简体中文回答。",
        "en": "Use English to answer.",
        "zh-TW": "使用繁體中文回答。"
    }
    language_instruction = language_instructions.get(language, language_instructions["zh-CN"])

    return (
    "你是一名**儒雅而严谨的 SQL 教师**。你的目标是通过专业、高标准的对话引导学生**在他自己的代码基础上**自主修正 SQL 错误。\n"
    "你的教学风格：**语气温和、态度严厉**。严厉体现在对逻辑严密性和数据库规范的坚持，温和体现在表达时的耐心与职业素养。\n\n"
    "【核心教学原则：最小改动】\n"
    "- 以**学生现有代码为起点**，指引学生在其代码上做**最小改动**达成正确、等价的写法。\n"
    "- 不要把标准答案当作「正确答案」来直接矫正学生，而是以**预期数据/结果**为蓝本，帮助学生理清逻辑、找准问题点，在他的代码上做尽量少的修改。\n"
    "- 始终围绕「你这段代码离正确还差什么、改哪里可以最少改动就等价」来引导，而不是让他重写整段 SQL。\n\n"
    "必须严格遵守以下规则：\n"
    "1. **先判断并明确指出错误类型**（例如：语法错误、逻辑错误、业务理解偏差等）。\n"
    "2. **绝对禁止**直接给出修改后的 SQL 完整代码，不能一次性输出从 SELECT 到结尾的完整语句；只能提示应修改的**局部**（如「这里的条件不对」「这里的排序方式不对」），引导学生自己写出等价代码。\n"
    "3. **【逻辑优先，不直接给关键字】判题后的首次提示（以及低/中支架）**：\n"
    "   - **只讲逻辑差异**，不直接说出 SQL 关键字或语法。例如：说「题目要求从大到小排序，你的写法没有体现排序」或「题目要求只取前几条，你的结果没有限制条数」，而不要说「要用 ORDER BY 和 DESC」「要用 LIMIT」。\n"
    "   - 可以说「题目要求降序而你写成了升序」「题目要求只返回前 3 条而你的结果没有限制条数」等，严禁给出 ORDER BY、DESC、LIMIT、GROUP BY 等关键字或与本题答案相关的代码片段。\n"
    "   - **鼓励学生提问**：用反问或邀请，如「你知道怎么实现从大到小吗？不知道可以在下方对话框问我。」「如果不知道用什么关键字限制条数，可以问我。」\n"
    "4. **【学生追问时可讲语法】当学生主动问**（如「降序用什么关键字」「怎么只取前几条」）时：\n"
    "   - 可以回答关键字、语法，并举例说明（例如用其他表、其他字段举例）。\n"
    "   - **严禁泄露本题答案**：举例必须与当前题目无关，不得给出可直接套用到本题的代码（如本题要求按 id 取前 3 条时，不得写 ORDER BY id DESC LIMIT 3）。\n"
    "5. 需要根据不同情境，灵活且严谨地选择并组合以下控制策略教学方法：\n"
    f"   **当前支架模式**：\n{scaffolding_instruction}\n"
    "   【苏格拉底式教学法】：反思提问、假设探究、结果预测、矛盾揭示。\n"
    "   【支架式教学】：低/中支架只讲逻辑、不直接给关键字；**高支架**可以适量写出关键字（如 ORDER BY、DESC、LIMIT）并举例介绍功能，举例必须与本题无关，严禁泄露本题完整代码。\n"
    "   【计算思维导向】：问题分解、反例生成、类比解释。\n"
    "6. **对学生保持“坚定且共情”的态度**：\n"
    "   - 严禁使用打击积极性的词汇（如“凭空想象”、“浪费时间”、“低级错误”）。\n"
    "   - 每次回答可以以 1～3 个启发式问题或邀请提问结尾，如「你知道怎么实现降序吗？不知道可以问我。」\n"
    "   - 结尾要自然多样，不要使用固定模板。\n"
    "7. **聚焦题目本身**：只关注正确性与简洁性，不引入索引、性能优化等宏大概念。\n"
    f"8. 回答语言：{language_instruction}\n"
    "9. 输出格式建议：先错误/逻辑诊断，再引导式提问或支架式提示，最后可邀请学生在对话框追问。\n"
    "10. **禁止向学生展示支架等级**：回复中不得出现「低支架」「中支架」「高支架」或「低/中/高支架引导」等字样，以免影响学生心态。若需要小节标题，请统一使用「逻辑引导」即可。\n"
    "11. **特殊情境**：学生因字段名错误卡死时，可引导用 DESC table_name 查看表结构，不嘲讽。\n"
    )

def _get_client():
    """初始化原生 AsyncOpenAI 客户端"""
    return AsyncOpenAI(
        api_key=_settings.AI_API_KEY,
        base_url=_settings.AI_BASE_URL,
    )

async def get_sql_hint(
    student_sql: str,
    question_content: str | None = None,
    is_correct: bool = False,
    hint_level: int = 1,
    failure_count: int = 0,
    error_message: str | None = None,
    language: str = "zh-CN",
    is_safety_blocked: bool = False,
    structural_errors: Optional[List[ASTError]] = None,
    hint_actions: Optional[List[HintAction]] = None,
    lambda_t: Optional[float] = None,
) -> SQLCheckResultSchema:
    """根据学生提交的 SQL 生成教学提示（原生 OpenAI 版）。"""

    # 1. 基础校验
    if not student_sql or not student_sql.strip():
        return SQLCheckResultSchema(
            diagnoses=[],
            overall_comment="请先提交一段你自己写的 SQL 语句。"
        )

    # 2. 准备 Prompt
    system_prompt = _build_system_prompt(hint_level, language, hint_actions, lambda_t)
    user_content_parts = []

    if structural_errors:
        error_summary = "\n".join([
            f"- {e.clause}相关的知识结构逻辑：系统判定的真实严重度权重 {e.severity:.1f}"
            for e in structural_errors
        ])
        user_content_parts.append(
            f"【算法传感器返回的高置信度逻辑与结构偏差】以下是学生 SQL 与预期结构的明确差异点（算法判定的核心方向，请以它为事实准绳针对性诱导发问）：\n{error_summary}\n"
        )

    if question_content:
        user_content_parts.append(f"【题目要求】\n{question_content}\n")

    user_content_parts.append(f"【学生提交的 SQL】\n{student_sql}\n")

    if is_correct:
        user_content_parts.append("【判题结果】SQL 正确。请给予鼓励并引导优化。\n")
    elif is_safety_blocked:
        user_content_parts.append(
            f"【判题结果】代码包含危险操作，系统已拒绝执行。\n{error_message or '练习环境仅允许 SELECT 查询。'}\n"
            "请明确指出学生 SQL 中哪里包含危险操作（如 DROP、DELETE、INSERT、UPDATE 等），并说明练习环境禁止改库/删库操作，仅允许 SELECT 查询。\n"
        )
    else:
        err = f"【执行错误】{error_message}\n" if error_message else "【判题结果】逻辑错误。\n"
        user_content_parts.append(err)

    if failure_count > 0:
        user_content_parts.append(f"【学习历史】已失败 {failure_count} 次。\n")

    user_content_parts.append(
        "请以自然对话的方式回复学生，就像一位老师在和学生面对面交流一样。不要使用JSON格式，直接使用自然的中文对话。"
    )

    # 构造原生消息格式
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(user_content_parts)}
    ]

    # 3. 获取客户端
    client = _get_client()

    try:
        model = (_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip()
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=_settings.AI_TEMPERATURE
        )
        
        # 4. 万能内容提取
        content = ""
        if isinstance(response, str):
            content = response
        elif hasattr(response, 'choices'):
            content = response.choices[0].message.content
        elif isinstance(response, dict):
            content = response.get('choices', [{}])[0].get('message', {}).get('content', str(response))
        else:
            content = str(response)
        
        # 5. 直接使用自然对话文本（不再解析JSON）
        # 去除可能的 Markdown 代码块标记
        clean_text = content.replace('```json', '').replace('```', '').strip()
        
        # 如果AI仍然返回了JSON格式（容错处理），尝试提取文本内容
        # 但优先使用原始文本，因为我们已经要求AI返回自然对话
        if clean_text.startswith('{') and clean_text.endswith('}'):
            try:
                parsed = json.loads(clean_text)
                # 如果确实是JSON，提取overall_comment
                if "overall_comment" in parsed:
                    clean_text = parsed["overall_comment"]
                    if parsed.get("diagnoses"):
                        # 将diagnoses也转换为自然语言
                        diagnoses_text = "\n".join([
                            f"• {d.get('error_type', '')}: {d.get('hint', '')}"
                            for d in parsed["diagnoses"]
                            if isinstance(d, dict)
                        ])
                        if diagnoses_text:
                            clean_text = f"{clean_text}\n\n{diagnoses_text}"
            except:
                # 解析失败，使用原始文本
                pass
        
        # 不再强制添加固定结尾，让AI自然生成多样化的结尾
        
        return SQLCheckResultSchema(
            diagnoses=[],
            overall_comment=clean_text
        )

    except Exception as e:
        print("--- get_sql_hint 异常 ---")
        traceback.print_exc()
        return SQLCheckResultSchema(
            diagnoses=[],
            overall_comment=f"AI 服务故障: {str(e)}"
        )

__all__ = ["get_sql_hint", "_build_system_prompt"]


async def chat_with_teacher(
    *,
    question_content: str | None,
    latest_student_sql: str | None,
    latest_is_correct: bool | None,
    latest_error_message: str | None,
    hint_level: int,
    failure_count: int,
    history: list[dict],
    user_message: str,
    language: str = "zh-CN",
) -> str:
    """多轮对话：在已有上下文下与 AI 教师继续交流（返回自然语言，不要求 JSON）。"""

    system_prompt = _build_system_prompt(hint_level, language)

    ctx_parts: list[str] = []
    if question_content:
        ctx_parts.append(f"【题目要求】\n{question_content}\n")
    if latest_student_sql:
        ctx_parts.append(f"【学生最新一次提交的 SQL】\n{latest_student_sql}\n")
    if latest_is_correct is True:
        ctx_parts.append("【最新判题结果】SQL 正确。可以引导学生进一步优化、思考性能与边界情况。\n")
    elif latest_is_correct is False:
        err = f"【最新执行错误】{latest_error_message}\n" if latest_error_message else "【最新判题结果】逻辑错误。\n"
        ctx_parts.append(err)
    if failure_count > 0:
        ctx_parts.append(f"【学习历史】该题已失败 {failure_count} 次。\n")
    ctx_parts.append(
        "现在请你作为 AI 教师，围绕学生的问题进行多轮引导式对话。"
        "始终以学生自己的 SQL 为起点，指引他在其代码基础上做**最小改动**达成等价写法；不要用标准答案去矫正他，而要以预期结果/数据为蓝本帮他理清逻辑。"
        "若学生追问关键字或语法（如「降序用什么」「怎么限制条数」），可回答并举例，但举例必须与本题无关，严禁泄露本题答案或可直接套用的完整 SQL。禁止给出完整可执行 SQL。"
    )

    # history: [{"role":"user"|"assistant","content":"..."}]
    # 只保留最近的若干条，避免过长
    trimmed_history = history[-20:] if len(history) > 20 else history

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(ctx_parts)},
        *trimmed_history,
        {"role": "user", "content": user_message},
    ]

    client = _get_client()
    model = (_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip()
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=_settings.AI_TEMPERATURE,
    )

    content = ""
    if isinstance(response, str):
        content = response
    elif hasattr(response, "choices"):
        content = response.choices[0].message.content
    elif isinstance(response, dict):
        content = response.get("choices", [{}])[0].get("message", {}).get("content", str(response))
    else:
        content = str(response)

    result = (content or "").strip()
    
    # 不再强制添加固定结尾，让AI自然生成多样化的结尾
    
    return result
