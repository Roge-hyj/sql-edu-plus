"""
AI Question Generator Service.

This module leverages OpenAI's Chat Completion API to dynamically generate SQL practice
questions, infer problem difficulty, extract schemas, translate descriptions, and verify
column alias requirements.
"""

import json
import re
import traceback
from typing import Any

from openai import AsyncOpenAI  # Official Async OpenAI client SDK for model requests

from settings import get_settings
from core.public_schema_preview import sanitize_schema_preview
from core.sql_knowledge_points import get_knowledge_point_by_id

_settings = get_settings()  # Global application configuration settings (API keys, endpoints, model configurations)


def _get_client() -> AsyncOpenAI:
    """
    Instantiates a unified AsyncOpenAI client based on project settings.

    Returns:
        AsyncOpenAI: Asynchronous OpenAI client.
    """
    return AsyncOpenAI(
        api_key=_settings.AI_API_KEY,   # Client authentication key
        base_url=_settings.AI_BASE_URL, # Optional proxy or third-party endpoint URL
    )


def _extract_response_text(response: Any) -> str:
    """Extract text from either Chat Completions or Responses SDK objects."""

    output_text = getattr(response, "output_text", None)
    if output_text is None and isinstance(response, dict):
        output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, dict):
                content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    text = getattr(block, "text", None)
                    if text is None and isinstance(block, dict):
                        text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        if parts:
            return "".join(parts).strip()

    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    message = getattr(first, "message", None)
    if message is None and isinstance(first, dict):
        message = first.get("message")
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else item.get("text", "")
            for item in content
            if isinstance(item, str) or isinstance(item, dict)
        ).strip()
    return ""


async def _create_model_text(
    client: AsyncOpenAI,
    *,
    system: str,
    user: str,
    temperature: float,
) -> str:
    """Call the configured wire protocol and return only response text."""

    if str(getattr(_settings, "AI_WIRE_API", "chat_completions")).lower() == "responses":
        response = await client.responses.create(
            model=(_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip(),
            instructions=system,
            input=user,
            store=False,
        )
    else:
        response = await client.chat.completions.create(
            model=(_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
    return _extract_response_text(response)


# Hint instructions injected into system prompts to guide LLM schema designs.
SCHEMA_HINT = """
判题环境仅允许 SELECT 查询，禁止 DROP/DELETE/INSERT/UPDATE 等。
可假设存在以下表（列名仅供参考，可自拟合理列名）：
- users: id, username, email, role, age, created_at
- orders: id, user_id, amount, status, created_at
- products: id, name, price, category_id
- categories: id, name
请生成可直接在标准 SQL 环境执行 of SELECT 语句，避免使用数据库特有函数（或注明为 MySQL/PostgreSQL 等）。
"""


async def generate_questions_for_knowledge_point(
    knowledge_point_id: str,
    count: int = 1,
) -> list[dict[str, Any]]:
    """
    Generates a list of SQL practice questions mapped to a specific knowledge point.

    Invokes OpenAI/LLM chat completions to get structural question JSON objects.

    Args:
        knowledge_point_id (str): Unique ID matching target SQL knowledge points.
        count (int, optional): Number of questions to generate (clamped between 1 and 5).

    Returns:
        list[dict[str, Any]]: Generated questions list, each containing title, content,
            correct_sql, difficulty, translations, and table schema preview.
    """
    # Enforce API bounds between 1 and 5 items per request
    count = max(1, min(5, count))

    # Retrieve metadata matching target knowledge point
    point = get_knowledge_point_by_id(knowledge_point_id)
    if not point:
        return []

    # Configure system prompt specifying strict JSON array format
    system = (
        "你是一位 SQL 教学出题专家。根据给定的知识点，生成适合学生练习的 SQL 题目。"
        "你必须且仅输出一个 JSON 数组，不要输出任何其他文字、说明或 markdown 标记。"
        "数组每个元素格式为："
        "{\"title\": \"题目标题(简体中文)\", \"content\": \"题目描述(简体中文)\", "
        "\"title_en\": \"题目标题(English)\", \"content_en\": \"题目描述(English)\", "
        "\"title_zh_tw\": \"題目標題(繁體中文)\", \"content_zh_tw\": \"題目描述(繁體中文)\", "
        "\"correct_sql\": \"标准答案 SELECT 语句\", \"difficulty\": 1-10 的整数, "
        "\"sql_dialect\": null, \"engine_version\": null, "
        "\"schema_preview\": {\"tables\": [{\"name\": \"表名\", \"columns\": [\"列1\", \"列2\"], \"rows\": [{\"列1\": 值1, \"列2\": 值2}]}]}}"
        "schema_preview 必须包含 correct_sql 中涉及的所有表（多表则输出多个表）。每个表的 columns 必须列出该表在 SQL 中出现的全部列名，确保学生能据此完成 SQL；示例数据 rows 可少（每表 2～4 行即可），但列名要齐全。"
        "title/content 必须为简体中文；title_zh_tw/content_zh_tw 为繁体中文；title_en/content_en 为英文。若不确定翻译，可直接复用简体中文。"
        "correct_sql 必须是合法的、仅包含 SELECT 的 SQL。"
        "difficulty：1 最简单，10 最难。"
        "【题目与标答严格一致】题目描述（content）必须与 correct_sql 的运算逻辑严格一致。"
        "若涉及容易歧义的概念（如「班级平均分」），必须在题目中明确写出计算方式，例如："
        "「先算每个学生的平均分，再对这些学生平均分求班级平均值」vs「班级所有成绩总分除以总科目数」；"
        "不得让题目描述含糊而 correct_sql 用一种实现，导致学生用等价另一种理解被判错。"
        + SCHEMA_HINT
    )
    
    kp_name = point["name"]
    kp_desc = point["description"]
    
    # Configure user query targeting the specific knowledge point name and details
    user = (
        "知识点：【" + kp_name + "】" + kp_desc + "】\n"
        "请生成 " + str(count) + " 道围绕该知识点的练习题。直接输出 JSON 数组，不要用代码块包裹。"
    )

    client = _get_client()
    try:
        # Request completion from the language model
        content = await _create_model_text(
            client,
            system=system,
            user=user,
            temperature=_settings.AI_TEMPERATURE,
        )
            
        if not content:
            return []

        # Remove surrounding markdown wrappers like ```json and ```
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        content = content.strip()
        
        # Load and parse output string into native Python object structure
        raw = json.loads(content)
        if not isinstance(raw, list):
            raw = [raw] if isinstance(raw, dict) else []

        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
                
            # Safely extract and check required fields
            title = (item.get("title") or "").strip()
            content_str = (item.get("content") or "").strip()
            title_en = (item.get("title_en") or "").strip() or None
            content_en = (item.get("content_en") or "").strip() or None
            title_zh_tw = (item.get("title_zh_tw") or "").strip() or None
            content_zh_tw = (item.get("content_zh_tw") or "").strip() or None
            correct_sql = (item.get("correct_sql") or "").strip()
            sql_dialect = _normalize_generated_sql_dialect(item.get("sql_dialect"))
            engine_version = (item.get("engine_version") or None)
            
            # Reformat difficulty to integer bounds [1, 10]
            difficulty = item.get("difficulty")
            if isinstance(difficulty, (int, float)):
                difficulty = int(max(1, min(10, difficulty)))
            else:
                difficulty = 5
                
            # Accept only valid queries that contain SELECT statements
            if title and content_str and correct_sql and correct_sql.strip().upper().startswith("SELECT"):
                schema_str = sanitize_schema_preview(
                    item.get("schema_preview"),
                    forbidden_sql=correct_sql,
                )
                        
                out.append({
                    "title": title[:200],
                    "content": content_str,
                    "title_en": title_en[:200] if isinstance(title_en, str) else None,
                    "content_en": content_en,
                    "title_zh_tw": title_zh_tw[:200] if isinstance(title_zh_tw, str) else None,
                    "content_zh_tw": content_zh_tw,
                    "correct_sql": correct_sql,
                    "sql_dialect": sql_dialect,
                    "engine_version": engine_version if isinstance(engine_version, str) else None,
                    "difficulty": difficulty,
                    "schema_preview": schema_str,
                })
        return out[: count]
    except Exception as e:
        print("--- generate_questions_for_knowledge_point 异常 ---")
        traceback.print_exc()
        return []


def _normalize_generated_sql_dialect(value: Any) -> str | None:
    from core.sql_dialect_resolver import UnsupportedSQLDialectError, normalize_sql_dialect

    try:
        return normalize_sql_dialect(value)
    except UnsupportedSQLDialectError:
        return None


async def infer_difficulty_from_question(content: str, correct_sql: str) -> int:
    """
    Infers the relative difficulty (1-10) of an SQL question based on its description and answer.

    Args:
        content (str): The description text of the SQL question.
        correct_sql (str): The standard solution query.

    Returns:
        int: The inferred difficulty level (1 to 10).
    """
    if not content or not correct_sql:
        return 5
        
    system = (
        "你是一位 SQL 教学专家。根据题目描述和标准答案 SQL，判断该题的难度等级。"
        "难度 1=最简单（如单表 SELECT），10=最难（如复杂窗口函数、CTE、多表连接）。"
        "只输出一个 1～10 的整数，不要输出任何其他文字。"
    )
    user = f"题目描述：\n{content[:800]}\n\n标准答案 SQL：\n{correct_sql[:500]}"
    
    try:
        client = _get_client()
        text = await _create_model_text(
            client,
            system=system,
            user=user,
            temperature=_settings.AI_TEMPERATURE,
        )
            
        if not text:
            return 5
            
        # Parse output value containing only digits
        digits = re.sub(r"[^0-9]", "", text)
        num = int(digits[:2] or "5") if digits else 5
        return max(1, min(10, num))
    except Exception:
        traceback.print_exc()
        return 5


async def infer_schema_preview_from_sql(content: str, correct_sql: str) -> str | None:
    """
    Constructs a schema preview JSON mapping based on query syntax and problem descriptions.

    Args:
        content (str): The text description of the SQL question.
        correct_sql (str): The standard solution query.

    Returns:
        str | None: Stringified tables schema JSON object, or None if generation fails.
    """
    if not content or not correct_sql:
        return None
        
    system = (
        "你是一位 SQL 教学专家。根据题目描述和标准答案 SQL，生成供学生参考的表结构预览。"
        "输出一个 JSON 对象，格式为：{\"tables\": [{\"name\": \"表名\", \"columns\": [\"列1\", \"列2\", ...], "
        "\"rows\": [{\"列1\": 值1, \"列2\": 值2}, ...]}]}"
        "要求：1）correct_sql 中涉及几个表就输出几个表，多表则 tables 数组有多项；"
        "2）每个表的 columns 必须包含该表在 SQL/题目中出现的全部列名，列名与 SQL 中一致，确保学生能据此完成 SQL；"
        "3）示例数据 rows 可以少（每表 2～4 行即可），但列名要齐全。只输出 JSON，不要输出任何其他文字。"
    )
    user = f"题目描述：\n{content[:800]}\n\n标准答案 SQL：\n{correct_sql[:800]}"
    
    try:
        client = _get_client()
        text = await _create_model_text(
            client,
            system=system,
            user=user,
            temperature=_settings.AI_TEMPERATURE,
        )
            
        if not text:
            return None
            
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        obj = json.loads(text)
        return sanitize_schema_preview(obj, forbidden_sql=correct_sql)
    except Exception:
        traceback.print_exc()
    return None


async def infer_question_i18n_from_zh(title: str, content: str) -> dict[str, str] | None:
    """
    Translates Simplified Chinese titles and question texts into English and Traditional Chinese.

    Args:
        title (str): Question title in Simplified Chinese.
        content (str): Question description in Simplified Chinese.

    Returns:
        dict[str, str] | None: Translation dict containing keys (title_en, content_en, etc.).
    """
    if not title or not content:
        return None
        
    system = (
        "You are a professional translator for an SQL education app. "
        "Translate the given Simplified Chinese question title and content into English and Traditional Chinese. "
        "Keep SQL keywords, identifiers, table/column names, numbers, and code blocks unchanged. "
        "Output ONLY a JSON object with keys: title_en, content_en, title_zh_tw, content_zh_tw. "
        "Do not output markdown, code fences, or extra text."
    )
    user = f"title(zh-CN): {title}\n\ncontent(zh-CN):\n{content}"
    
    try:
        client = _get_client()
        text = await _create_model_text(
            client,
            system=system,
            user=user,
            temperature=_settings.AI_TEMPERATURE,
        )
            
        if not text:
            return None
            
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        obj = json.loads(text)
        if not isinstance(obj, dict):
            return None
            
        keys = ["title_en", "content_en", "title_zh_tw", "content_zh_tw"]
        out: dict[str, str] = {}
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
        if len(out) != 4:
            return None
        return out
    except Exception:
        traceback.print_exc()
        return None


async def infer_alias_requirement_from_content(content: str) -> bool:
    """
    Determines if a question description explicitly requires using output column aliases.

    Args:
        content (str): The text description of the SQL question.

    Returns:
        bool: True if aliases are explicitly required, False otherwise.
    """
    if not content or not content.strip():
        return False
        
    system = (
        "你是一位 SQL 教学专家。判断以下题目描述是否明确要求学生为查询结果的列起特定的别名（alias）。\n"
        "只有当题目明确要求学生必须使用 AS 关键字为列起别名、或明确指定了输出列的名称时，才回答 true。\n"
        "如果题目只是普通的查询要求，没有提到别名/alias/列名要求，回答 false。\n"
        "只输出 true 或 false，不要输出任何其他文字。"
    )
    user = f"题目描述：\n{content[:1000]}"
    
    try:
        client = _get_client()
        text = (
            await _create_model_text(
                client,
                system=system,
                user=user,
                temperature=0.1,
            )
        ).lower()
        return text == "true"
    except Exception:
        traceback.print_exc()
        return False


__all__ = [
    "generate_questions_for_knowledge_point",
    "infer_difficulty_from_question",
    "infer_schema_preview_from_sql",
    "infer_question_i18n_from_zh",
    "infer_alias_requirement_from_content",
]
