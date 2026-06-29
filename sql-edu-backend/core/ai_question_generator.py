"""根据 SQL 知识点由 AI 生成题目（教师端使用）。"""

import json
import re
import traceback
from typing import Any

from openai import AsyncOpenAI  # 官方 OpenAI SDK 的异步客户端，用来请求大语言模型

from settings import get_settings
from core.sql_knowledge_points import get_knowledge_point_by_id

_settings = get_settings()  # 读取全局配置（在 settings.py 中定义），比如 API Key、模型名称等


def _get_client() -> AsyncOpenAI:
    """封装一个统一的 OpenAI 异步客户端。

    这样如果以后要换别的模型服务，只需要改这一处，不用到处改代码。
    """
    return AsyncOpenAI(
        api_key=_settings.AI_API_KEY,   # 调用大模型的密钥
        base_url=_settings.AI_BASE_URL, # 模型服务地址（可以是官方或自建代理）
    )


# 判题环境仅允许 SELECT；提示 AI 使用常见教学表结构。
# 这段文字会拼到 system prompt 里，让大模型“知道”判题环境里有哪些表、能用什么 SQL。
SCHEMA_HINT = """
判题环境仅允许 SELECT 查询，禁止 DROP/DELETE/INSERT/UPDATE 等。
可假设存在以下表（列名仅供参考，可自拟合理列名）：
- users: id, username, email, role, age, created_at
- orders: id, user_id, amount, status, created_at
- products: id, name, price, category_id
- categories: id, name
请生成可直接在标准 SQL 环境执行的 SELECT 语句，避免使用数据库特有函数（或注明为 MySQL/PostgreSQL 等）。
"""


async def generate_questions_for_knowledge_point(
    knowledge_point_id: str,
    count: int = 1,
) -> list[dict[str, Any]]:
    """根据某个「知识点」让 AI 生成若干道练习题。

    :param knowledge_point_id: 知识点 id（见 sql_knowledge_points）
    :param count: 生成数量，1～5
    :return: [{"title": str, "content": str, "correct_sql": str, "difficulty": int}, ...]
    """
    # 为了防止一次性生成太多/太少，这里把用户输入限制在 1～5 之间
    count = max(1, min(5, count))

    # 根据 id 找到对应的知识点配置（名称、描述等）
    point = get_knowledge_point_by_id(knowledge_point_id)
    if not point:
        return []

    # system prompt：告诉大模型「你是谁、要做什么、输出格式是什么」
    system = (
        "你是一位 SQL 教学出题专家。根据给定的知识点，生成适合学生练习的 SQL 题目。"
        "你必须且仅输出一个 JSON 数组，不要输出任何其他文字、说明或 markdown 标记。"
        "数组每个元素格式为："
        "{\"title\": \"题目标题(简体中文)\", \"content\": \"题目描述(简体中文)\", "
        "\"title_en\": \"题目标题(English)\", \"content_en\": \"题目描述(English)\", "
        "\"title_zh_tw\": \"題目標題(繁體中文)\", \"content_zh_tw\": \"題目描述(繁體中文)\", "
        "\"correct_sql\": \"标准答案 SELECT 语句\", \"difficulty\": 1-10 的整数, "
        "\"schema_preview\": {\"tables\": [{\"name\": \"表名\", \"columns\": [\"列1\", \"列2\"], \"rows\": [{\"列1\": 值1, \"列2\": 值2}]}]}}"
        "schema_preview 必须包含 correct_sql 中涉及的所有表（多表则输出多个表）。每个表的 columns 必须列出该表在 SQL 中出现的全部列名，确保学生能据此完成 SQL；示例数据 rows 可少（每表 2～4 行即可），但列名要齐全。"
        "title/content 必须为简体中文；title_zh_tw/content_zh_tw 为繁体中文；title_en/content_en 为英文。若不确定翻译，可直接复用简体中文。"
        "correct_sql 必须是合法的、仅包含 SELECT 的 SQL。"
        "difficulty：1 最简单，10 最难。"
        "【题目与标答严格一致】题目描述（content）必须与 correct_sql 的运算逻辑严格一致。"
        "若涉及容易歧义的概念（如「班级平均分」），必须在题目中明确写出计算方式，例如："
        "「先算每个学生的平均分，再对这些学生平均分求班级平均值」vs「班级所有成绩总分除以总科目数」；"
        "不得让题目描述含糊而 correct_sql 用一种实现，导致学生用等价另一种理解被判错。"
        + SCHEMA_HINT  # 在结尾拼上上面的 SCHEMA_HINT，让 AI 知道判题环境的表结构大致长什么样
    )
    # 从知识点中拿出名称和描述，用来喂给大模型
    kp_name = point["name"]
    kp_desc = point["description"]
    # user prompt：具体告诉大模型「这个知识点是什么，请你出几道题」
    user = (
        "知识点：【" + kp_name + "】" + kp_desc + "】\n"
        "请生成 " + str(count) + " 道围绕该知识点的练习题。直接输出 JSON 数组，不要用代码块包裹。"
    )

    # 初始化大模型客户端
    client = _get_client()
    try:
        # 调用大模型生成题目，这里使用的是 Chat Completion 接口
        response = await client.chat.completions.create(
            model=(_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=_settings.AI_TEMPERATURE,
        )
        # 为了兼容不同 SDK 返回类型，这里做了比较“保险”的解析方式
        content = ""
        if hasattr(response, "choices") and response.choices:
            content = (response.choices[0].message.content or "").strip()
        elif isinstance(response, dict):
            content = (response.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        # 如果模型没有返回内容，直接返回空列表
        if not content:
            return []

        # 有些模型喜欢自动加 ```json ``` 包裹，这里统一去掉代码块标记
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        content = content.strip()
        # 将模型输出的 JSON 字符串解析成 Python 对象
        raw = json.loads(content)
        if not isinstance(raw, list):
            raw = [raw] if isinstance(raw, dict) else []

        # out 用来存放整理后的题目信息，最终返回给调用方
        out = []
        for item in raw:
            # 每一项必须是字典，否则跳过
            if not isinstance(item, dict):
                continue
            # 下面这一大段都是“取字段 + 安全兜底”的逻辑：
            # - 用 get 防止 KeyError
            # - strip() 去空格
            # - 对可能缺失的字段给默认值
            title = (item.get("title") or "").strip()
            content_str = (item.get("content") or "").strip()
            title_en = (item.get("title_en") or "").strip() or None
            content_en = (item.get("content_en") or "").strip() or None
            title_zh_tw = (item.get("title_zh_tw") or "").strip() or None
            content_zh_tw = (item.get("content_zh_tw") or "").strip() or None
            correct_sql = (item.get("correct_sql") or "").strip()
            # 难度可能是 int / float / 甚至缺失，这里都统一成 1～10 的整数
            difficulty = item.get("difficulty")
            if isinstance(difficulty, (int, float)):
                difficulty = int(max(1, min(10, difficulty)))
            else:
                difficulty = 5
            # 只有当「题目标题」「描述」「标准答案 SQL」都不为空，且答案是 SELECT 开头时才认为是合法题目
            if title and content_str and correct_sql and correct_sql.strip().upper().startswith("SELECT"):
                schema_preview = item.get("schema_preview")
                schema_str = None
                if isinstance(schema_preview, dict) and schema_preview.get("tables"):
                    try:
                        schema_str = json.dumps(schema_preview, ensure_ascii=False)
                    except Exception:
                        schema_str = None
                # 对标题截断是为了防止数据库字段溢出
                out.append({
                    "title": title[:200],
                    "content": content_str,
                    "title_en": title_en[:200] if isinstance(title_en, str) else None,
                    "content_en": content_en,
                    "title_zh_tw": title_zh_tw[:200] if isinstance(title_zh_tw, str) else None,
                    "content_zh_tw": content_zh_tw,
                    "correct_sql": correct_sql,
                    "difficulty": difficulty,
                    "schema_preview": schema_str,
                })
        # 最多只返回用户要求数量的题目
        return out[: count]
    except Exception as e:
        # 为了方便排查问题，这里打印异常堆栈，但对调用方只返回空列表
        print("--- generate_questions_for_knowledge_point 异常 ---")
        traceback.print_exc()
        return []


async def infer_difficulty_from_question(content: str, correct_sql: str) -> int:
    """根据题目描述和标准答案 SQL，由 AI 推断难度（1～10）。

    可以理解为「自动打分」：题目越复杂、SQL 越高级，难度就会越高。
    """
    if not content or not correct_sql:
        return 5
    # system prompt：告诉 AI 只需要给一个 1～10 的整数
    system = (
        "你是一位 SQL 教学专家。根据题目描述和标准答案 SQL，判断该题的难度等级。"
        "难度 1=最简单（如单表 SELECT），10=最难（如复杂窗口函数、CTE、多表连接）。"
        "只输出一个 1～10 的整数，不要输出任何其他文字。"
    )
    # user prompt：把题面和标准答案同时给到 AI
    user = f"题目描述：\n{content[:800]}\n\n标准答案 SQL：\n{correct_sql[:500]}"
    try:
        # 复用统一的客户端
        client = _get_client()
        response = await client.chat.completions.create(
            model=(_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=_settings.AI_TEMPERATURE,
        )
        # 兼容性解析：不同版本的 SDK 返回格式可能略有差异
        text = ""
        if hasattr(response, "choices") and response.choices:
            text = (response.choices[0].message.content or "").strip()
        # 如果 AI 没返回东西，就退回到默认难度 5
        if not text:
            return 5
        # 只保留数字字符（防止 AI 多说话），取前两位拼成整数
        digits = re.sub(r"[^0-9]", "", text)
        num = int(digits[:2] or "5") if digits else 5
        return max(1, min(10, num))
    except Exception:
        traceback.print_exc()
        return 5


async def infer_schema_preview_from_sql(content: str, correct_sql: str) -> str | None:
    """根据题目描述和标准答案 SQL，由 AI 生成表结构预览（JSON 字符串）。

    前端会用这个「表结构预览」来给学生展示“有哪些表、有哪些字段、示例数据是什么样”。
    """
    if not content or not correct_sql:
        return None
    # system prompt：约定好 schema_preview 的 JSON 格式
    system = (
        "你是一位 SQL 教学专家。根据题目描述和标准答案 SQL，生成供学生参考的表结构预览。"
        "输出一个 JSON 对象，格式为：{\"tables\": [{\"name\": \"表名\", \"columns\": [\"列1\", \"列2\", ...], "
        "\"rows\": [{\"列1\": 值1, \"列2\": 值2}, ...]}]}"
        "要求：1）correct_sql 中涉及几个表就输出几个表，多表则 tables 数组有多项；"
        "2）每个表的 columns 必须包含该表在 SQL/题目中出现的全部列名，列名与 SQL 中一致，确保学生能据此完成 SQL；"
        "3）示例数据 rows 可以少（每表 2～4 行即可），但列名要齐全。只输出 JSON，不要输出任何其他文字。"
    )
    # user prompt：同样把题面和标准答案都给 AI
    user = f"题目描述：\n{content[:800]}\n\n标准答案 SQL：\n{correct_sql[:800]}"
    try:
        client = _get_client()
        response = await client.chat.completions.create(
            model=(_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=_settings.AI_TEMPERATURE,
        )
        text = ""
        if hasattr(response, "choices") and response.choices:
            text = (response.choices[0].message.content or "").strip()
        # 没有返回则不生成 schema_preview
        if not text:
            return None
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        # 把 AI 回复解析成 Python 对象，确保是我们期望的字典格式
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("tables"):
            return json.dumps(obj, ensure_ascii=False)
    except Exception:
        traceback.print_exc()
    return None


async def infer_question_i18n_from_zh(title: str, content: str) -> dict[str, str] | None:
    """将中文题面翻译为英文与繁体中文（仅题目标题/描述）。

    返回：{"title_en": "...", "content_en": "...", "title_zh_tw": "...", "content_zh_tw": "..."}
    """
    if not title or not content:
        return None
    # system prompt：把角色设定为“专业翻译”，并且强调「SQL 关键字和表字段名不要翻译」
    system = (
        "You are a professional translator for an SQL education app. "
        "Translate the given Simplified Chinese question title and content into English and Traditional Chinese. "
        "Keep SQL keywords, identifiers, table/column names, numbers, and code blocks unchanged. "
        "Output ONLY a JSON object with keys: title_en, content_en, title_zh_tw, content_zh_tw. "
        "Do not output markdown, code fences, or extra text."
    )
    # user prompt：把原始中文标题和内容一并给到 AI
    user = f"title(zh-CN): {title}\n\ncontent(zh-CN):\n{content}"
    try:
        client = _get_client()
        response = await client.chat.completions.create(
            model=(_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=_settings.AI_TEMPERATURE,
        )
        text = ""
        if hasattr(response, "choices") and response.choices:
            text = (response.choices[0].message.content or "").strip()
        # 没有返回内容则认为翻译失败
        if not text:
            return None
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        # 期望 AI 输出的是一个 JSON 对象，这里先解析
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
    """由 AI 判断题目描述是否要求学生使用特定的列别名。

    :param content: 题目描述
    :return: True 表示题目要求使用特定别名，False 表示不要求
    """
    # 没有题目描述时，默认认为「没有别名要求」
    if not content or not content.strip():
        return False
    # system prompt：要求 AI 只回答 true/false，方便后端直接解析
    system = (
        "你是一位 SQL 教学专家。判断以下题目描述是否明确要求学生为查询结果的列起特定的别名（alias）。\n"
        "只有当题目明确要求学生必须使用 AS 关键字为列起别名、或明确指定了输出列的名称时，才回答 true。\n"
        "如果题目只是普通的查询要求，没有提到别名/alias/列名要求，回答 false。\n"
        "只输出 true 或 false，不要输出任何其他文字。"
    )
    # user prompt：把题目描述的前 1000 字符给 AI（足够判断是否提到了别名）
    user = f"题目描述：\n{content[:1000]}"
    try:
        client = _get_client()
        response = await client.chat.completions.create(
            model=(_settings.AI_MODEL_NAME or "gpt-3.5-turbo").strip(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,  # 低温度，确保一致性
        )
        text = ""
        if hasattr(response, "choices") and response.choices:
            text = (response.choices[0].message.content or "").strip().lower()
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
