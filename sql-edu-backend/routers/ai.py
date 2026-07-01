"""
AI 相关路由（/ai）

包含：
- `/ai/sql-hint`: 仅生成提示（已下线，返回静态内容）
- `/ai/check-sql`: 判题 + 本地 Socratic 诊断提示（Stage 1 Observe 驱动）
- `/ai/mastery-radar`: 获取学习画像（已简化为返回静态掌握度）
- `/ai/chat/*`: 多轮对话历史与本地 Socratic 辅导
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
from typing import Optional, List, Dict, Any

from core.sql_judge import SQLJudgeService, SQLJudgeError, SQLSafetyError
from core.judge_setup import generate_init_sql_from_schema_preview, execute_setup_sql
from repository import QuestionRepository, SubmissionRepository, ChatRepository, UserRepository
from core.experience_service import compute_xp_gain, get_level_from_total
from schemas.submission import SubmissionCreate, SubmissionOut
from schemas.chat import ChatMessageOut, ChatSendIn, ChatSendOut
from dependencies import get_session
from core.auth import AuthHandler

# 数学闭环第一阶段（Observe/感知）核心引擎库
from core.ast_analyzer import compute_error_vector, infer_knowledge_points_from_sql
from core.error_attribution import evidence_weights_from_observation, KPAttribution
from schemas.agent import SQLCheckResultSchema

router = APIRouter(prefix="/ai", tags=["ai"])
auth_handler = AuthHandler()

class SQLRequest(BaseModel):
    sql: str

class SQLCheckRequest(BaseModel):
    student_sql: str
    question_id: int
    language: str = "zh-CN"
    challenge_mode: bool = False

class SQLCheckResponse(BaseModel):
    is_correct: bool
    hint: dict
    submission_id: int
    error_message: str | None = None
    is_safety_blocked: bool = False
    earned_experience: int | None = None
    level_up: bool = False
    new_level: int | None = None
    lambda_t: float | None = None
    observation: dict | None = None
    error_attributions: list[dict] = Field(default_factory=list)


def _generate_local_feedback(
    is_correct: bool,
    is_safety_blocked: bool,
    error_message: str | None,
    attributions: List[KPAttribution],
    language: str = "zh-CN"
) -> str:
    if is_safety_blocked:
        if language == "en":
            return f"Security Blocked: Direct database modification operations (e.g. DROP, DELETE, INSERT, UPDATE) are prohibited.\nDetails: {error_message or 'Only SELECT queries are allowed.'}"
        elif language == "zh-TW":
            return f"安全攔截：禁止直接執行 DROP/DELETE/INSERT/UPDATE 等修改或刪除數據庫操作。\n詳情：{error_message or '練習環境僅允許 SELECT 查詢。'}"
        else:
            return f"安全拦截：禁止直接执行 DROP/DELETE/INSERT/UPDATE 等修改或删除数据库操作。\n详情：{error_message or '练习环境仅允许 SELECT 查询。'}"

    if is_correct:
        if language == "en":
            return "Congratulations! Your query is correct and equivalent to the standard solution."
        elif language == "zh-TW":
            return "恭喜你！你的作答在沙盒測試中與標準答案完全等效，執行結果正確！"
        else:
            return "恭喜你！你的作答在沙盒测试中与标准答案完全等效，执行结果正确！"

    if not attributions:
        if error_message:
            if language == "en":
                return f"Incorrect. Sandbox execution returned an error:\n\n```\n{error_message}\n```\n\nPlease check your query syntax and logic."
            elif language == "zh-TW":
                return f"作答不正確。執行沙盒返回了錯誤信息：\n\n```\n{error_message}\n```\n\n請仔細檢查你的 SQL 語法與邏輯。"
            else:
                return f"作答不正确。执行沙盒返回了错误信息：\n\n```\n{error_message}\n```\n\n请仔细检查你的 SQL 语法与逻辑。"
        
        if language == "en":
            return "Incorrect. The execution output does not match the standard solution. Please check your SELECT projection, JOIN conditions, or WHERE filters."
        elif language == "zh-TW":
            return "作答不正確。執行結果與標準答案不匹配。請重新檢查 SELECT 投影項、JOIN 條件或 WHERE 過濾子句的邏輯是否完全正確。"
        else:
            return "作答不正确。执行结果与标准答案不匹配。请重新检查 SELECT 投影项、JOIN 条件或 WHERE 过滤子句的逻辑是否完全正确。"

    # 针对不正确且有归因的情况
    if language == "en":
        lines = ["I analyzed your submission and found the following issues:"]
        for attr in attributions:
            lines.append(f"- **{attr.clause}** aspect: {attr.detail}")
        lines.append("\nPlease revise your query based on these points and try again!")
    elif language == "zh-TW":
        lines = ["分析了你的提交後，發現以下幾個需要注意的問題："]
        for attr in attributions:
            lines.append(f"- **{attr.clause}** 方面：{attr.detail}")
        lines.append("\n請根據上述提示修改你的 SQL 後重新提交。")
    else:
        lines = ["分析了你的提交后，发现以下几个需要注意的问题："]
        for attr in attributions:
            lines.append(f"- **{attr.clause}** 方面：{attr.detail}")
        lines.append("\n请根据上述提示修改你的 SQL 后重新提交。")

    return "\n".join(lines)


@router.post("/sql-hint")
async def sql_hint(payload: SQLRequest):
    """获取 SQL 提示（已下线，仅返回静态内容）。"""
    return {"hint": {
        "diagnoses": [],
        "overall_comment": "AI 提示服务已下线，练习界面现在采用本地分析引擎生成更精准的实时诊断提示。"
    }}


@router.get("/mastery-radar")
async def get_mastery_radar(
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """取得当前学生的知识掌握度画像（简化为静态 0.5 掌握度以防止前端组件报错）。"""
    from core.error_attribution import KP_META
    state_map = {kp_id: 0.5 for kp_id in KP_META.keys()}
    return {"mastery_state": state_map}


@router.post("/check-sql", response_model=SQLCheckResponse)
async def check_sql(
    payload: SQLCheckRequest,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """检查学生提交的 SQL 是否正确，并生成本地诊断提示。"""
    # 1. 查询题目
    question_repo = QuestionRepository(session)
    question = await question_repo.get_by_id(payload.question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {payload.question_id} 不存在",
        )

    # 1.1 安全性与语法检查 (T_SAFE & T_SYNTAX)
    judge_service = SQLJudgeService(session)
    safe, keyword = judge_service._check_sql_safety(payload.student_sql)
    is_safety_blocked = not safe

    is_syntax_error = False
    syntax_error_msg = ""
    if not is_safety_blocked:
        import sqlglot
        from sqlglot import ErrorLevel
        try:
            sqlglot.parse_one(payload.student_sql, dialect="mysql", error_level=ErrorLevel.RAISE)
        except Exception as e:
            is_syntax_error = True
            syntax_error_msg = str(e)

    submission_repo = SubmissionRepository(session)
    failure_count = await submission_repo.get_failure_count(user_id, payload.question_id)
    correct_count_before = await submission_repo.get_correct_count(user_id, payload.question_id)

    # 如果是语法错误，直接调用语法纠错提示并结束 (O_SYNTAX_EXIT)
    if is_syntax_error:
        if payload.language == "en":
            ai_hint_text = f"Your SQL has syntax errors. Parser reported:\n\n```\n{syntax_error_msg}\n```\n\nPlease check keywords, parentheses, or commas."
        elif payload.language == "zh-TW":
            ai_hint_text = f"你的 SQL 語法書寫不正確，解析報錯：\n\n```\n{syntax_error_msg}\n```\n\n請檢查拼寫、括號匹配或逗號等基礎語法。"
        else:
            ai_hint_text = f"你的 SQL 语法书写不正确，解析报错：\n\n```\n{syntax_error_msg}\n```\n\n请检查拼写、括号匹配或逗号等基础语法。"

        # 保存提交记录与对话历史
        submission_data = SubmissionCreate(
            user_id=user_id,
            question_id=payload.question_id,
            student_sql=payload.student_sql,
            ai_hint=ai_hint_text,
            is_correct=False,
            hint_level=1,
        )
        submission = await submission_repo.create(submission_data)

        chat_repo = ChatRepository(session)
        await chat_repo.add_message(
            user_id=user_id,
            question_id=payload.question_id,
            role="system",
            content="【新一轮提交】结果：不正确 (语法错误)",
        )
        await chat_repo.add_message(
            user_id=user_id,
            question_id=payload.question_id,
            role="user",
            content=f"我提交的 SQL：\n\n```sql\n{payload.student_sql}\n```",
        )
        await chat_repo.add_message(
            user_id=user_id,
            question_id=payload.question_id,
            role="assistant",
            content=ai_hint_text,
        )
        await session.commit()

        ai_hint_result = SQLCheckResultSchema(
            diagnoses=[],
            overall_comment=ai_hint_text
        )

        return SQLCheckResponse(
            is_correct=False,
            hint=ai_hint_result.model_dump(),
            submission_id=submission.id,
            error_message=f"SQL 语法错误: {syntax_error_msg}",
            is_safety_blocked=False,
            earned_experience=None,
            level_up=False,
            new_level=None,
            lambda_t=None,
            observation=None,
            error_attributions=[],
        )

    # 1.5 判题前自动建表
    init_sql = generate_init_sql_from_schema_preview(getattr(question, "schema_preview", None))
    if init_sql:
        await execute_setup_sql(session, init_sql)

    # 2. SQL 判题
    is_correct = False
    error_message = None
    judge_detail = None
    observation = None
    error_attributions: list[dict] = []

    try:
        if is_safety_blocked:
            error_message = (
                f"SQL 包含危险操作（检测到关键字：{keyword.upper()}）。练习环境仅允许 SELECT 查询，禁止 DROP/DELETE/INSERT/UPDATE 等改库删库操作。"
                if keyword
                else "SQL 必须以 SELECT 开头。练习环境仅允许 SELECT 查询语句。"
            )
            is_correct = False
        else:
            required_cols = getattr(question, "required_output_columns", None)
            judge_detail = await judge_service.judge_sql_detailed(
                payload.student_sql, question.correct_sql, required_output_columns=required_cols
            )
            is_correct = judge_detail["is_correct"]
            error_message = judge_detail["error_message"]
    except SQLSafetyError as e:
        error_message = str(e)
        is_correct = False
        is_safety_blocked = True
    except SQLJudgeError as e:
        error_message = str(e)
        is_correct = False

    # ------------------------------------------------------------
    # Phase 1: 采集观察数据与归因 (Observe/感知)
    # ------------------------------------------------------------
    if not is_safety_blocked:
        # Convert schema JSON to parseval compact schema string
        schema_preview_str = getattr(question, "schema_preview", None)
        parseval_schema = ""
        if schema_preview_str:
            try:
                import json
                schema_json = json.loads(schema_preview_str)
                tables = schema_json.get("tables", [])
                parseval_schema = "; ".join(
                    f"{tbl.get('name')}({', '.join(tbl.get('columns', []))})"
                    for tbl in tables if tbl.get('name') and tbl.get('columns')
                )
            except Exception:
                pass

        mutation_detail = None
        if parseval_schema:
            try:
                from core.parseval_data_generator import generate_and_compare
                sandbox_run = generate_and_compare(
                    schema_text=parseval_schema,
                    standard_sql=question.correct_sql,
                    student_sql=payload.student_sql,
                )
                if sandbox_run and sandbox_run.executed:
                    non_null_judge_detail = judge_detail or {}
                    if "comparison" not in non_null_judge_detail:
                        non_null_judge_detail["comparison"] = {}
                    non_null_judge_detail["comparison"]["is_equivalent_on_generated_data"] = sandbox_run.is_equivalent
                    non_null_judge_detail["is_equivalent_on_generated_data"] = sandbox_run.is_equivalent
                    judge_detail = non_null_judge_detail
                    mutation_detail = sandbox_run.mutation_evidence
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"ParSEval verification failed: {e}", exc_info=True)

        attribution_result = evidence_weights_from_observation(
            student_sql=payload.student_sql,
            answer_sql=question.correct_sql,
            is_correct=is_correct,
            error_message=error_message,
            judge_detail=judge_detail,
            mutation_detail=mutation_detail,
        )
        observation = attribution_result.observation
        error_attributions = [item.to_dict() for item in attribution_result.attributions]
        attributions_list = attribution_result.attributions
    else:
        attributions_list = []

    # 3. 本地生成诊断提示文本 (Socratic Local Feedback)
    ai_hint_text = _generate_local_feedback(
        is_correct=is_correct,
        is_safety_blocked=is_safety_blocked,
        error_message=error_message,
        attributions=attributions_list,
        language=payload.language
    )

    # 经验值发放结算
    chat_repo = ChatRepository(session)
    chat_count_for_xp = await chat_repo.count_messages_for_user_question(user_id, payload.question_id)

    earned_experience = None
    level_up = False
    new_level = None
    if is_correct and correct_count_before == 0:
        xp = compute_xp_gain(
            question_difficulty=max(1, min(10, question.difficulty)),
            chat_count=chat_count_for_xp,
            wrong_attempts_before_correct=failure_count,
            challenge_mode=payload.challenge_mode,
        )
        user_repo = UserRepository(session)
        user = await user_repo.get_by_id(user_id)
        if user is not None:
            prev_total = getattr(user, "total_experience", 0) or 0
            new_total = prev_total + xp
            user.total_experience = new_total
            prev_level, _, _ = get_level_from_total(prev_total)
            cur_level, _, xp_next = get_level_from_total(new_total)
            earned_experience = xp
            level_up = cur_level > prev_level
            new_level = cur_level if level_up else None

    # 6. 保存提交记录
    submission_data = SubmissionCreate(
        user_id=user_id,
        question_id=payload.question_id,
        student_sql=payload.student_sql,
        ai_hint=ai_hint_text,
        is_correct=is_correct,
        hint_level=1,
    )
    submission = await submission_repo.create(submission_data)

    if is_safety_blocked:
        system_result = "【新一轮提交】代码包含危险操作，系统已拒绝执行。"
    else:
        system_result = f"【新一轮提交】结果：{'正确' if is_correct else '不正确'}"

    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="system",
        content=system_result,
    )
    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="user",
        content=f"我提交的 SQL：\n\n```sql\n{payload.student_sql}\n```",
    )
    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="assistant",
        content=ai_hint_text,
    )

    await session.commit()

    ai_hint_result = SQLCheckResultSchema(
        diagnoses=[],
        overall_comment=ai_hint_text
    )

    return SQLCheckResponse(
        is_correct=is_correct,
        hint=ai_hint_result.model_dump(),
        submission_id=submission.id,
        error_message=error_message,
        is_safety_blocked=is_safety_blocked,
        earned_experience=earned_experience,
        level_up=level_up,
        new_level=new_level,
        lambda_t=None,
        observation=observation,
        error_attributions=error_attributions,
    )


@router.get("/chat/messages", response_model=list[ChatMessageOut])
async def get_chat_messages(
    question_id: int = Query(..., description="题目 ID"),
    limit: int = Query(50, ge=1, le=200, description="限制数量"),
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    chat_repo = ChatRepository(session)
    msgs = await chat_repo.list_messages(user_id=user_id, question_id=question_id, limit=limit)
    return msgs


@router.delete("/chat/messages")
async def clear_chat_messages(
    question_id: int = Query(..., description="题目 ID"),
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """清除当前用户在该题目下的所有对话历史。"""
    chat_repo = ChatRepository(session)
    deleted = await chat_repo.delete_messages_by_user_question(user_id=user_id, question_id=question_id)
    await session.commit()
    return {"deleted": deleted}


@router.post("/chat", response_model=ChatSendOut)
async def chat(
    payload: ChatSendIn,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    # 题目上下文
    question_repo = QuestionRepository(session)
    question = await question_repo.get_by_id(payload.question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {payload.question_id} 不存在",
        )

    # 最近一次提交（可选）
    submission_repo = SubmissionRepository(session)
    latest_list = await submission_repo.get_user_submissions(user_id, payload.question_id, limit=1)
    latest = latest_list[0] if latest_list else None

    # 先写入用户消息
    chat_repo = ChatRepository(session)
    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="user",
        content=payload.message,
    )

    # 本地生成苏格拉底助教对话回复
    if latest:
        if latest.is_correct:
            if payload.language == "en":
                reply = "Great job! Your previous SQL submission is correct. Feel free to ask if you have any questions or optimizations."
            elif payload.language == "zh-TW":
                reply = "太棒了！你的上一輪 SQL 提交是完全正確的。如果有關於該題目的優化或其他疑問，可以隨時與我交流。"
            else:
                reply = "太棒了！你的上一轮 SQL 提交是完全正确的。如果有关于该题目的优化或其他疑问，可以随时跟我交流。"
        else:
            # 提取上一次的本地 Socratic 提示信息并反馈
            if payload.language == "en":
                reply = f"Hello! I am your SQL teaching assistant. Regarding your last SQL submission, we identified the following structural or logical issues:\n\n{latest.ai_hint}\n\nPlease revise your query based on this analysis and submit again."
            elif payload.language == "zh-TW":
                reply = f"你好！我是你的 SQL 助教。關於你上一輪提交的 SQL 語句，我們發現了以下結構或邏輯問題：\n\n{latest.ai_hint}\n\n你可以根據提示重點檢查對應的子句和過濾條件。修改完後，請重新提交運行。"
            else:
                reply = f"你好！我是你的 SQL 助教。关于你上一轮提交的 SQL 语句，我们发现了以下结构或逻辑问题：\n\n{latest.ai_hint}\n\n你可以根据提示重点检查对应的子句和过滤条件。修改完后，请重新提交运行。"
    else:
        if payload.language == "en":
            reply = "Hello! I am your SQL teaching assistant. You haven't submitted any queries for this question yet. Please write your SQL and click 'Submit & Run', and I will analyze it for you."
        elif payload.language == "zh-TW":
            reply = "你好！我是你的 SQL 助教。你還沒有針對這道題提交任何作答。請先編寫你的查詢並點擊『提交運行』，我將為你分析具體問題。"
        else:
            reply = "你好！我是你的 SQL 助教。你还没有针对这道题提交任何作答。请先编写你的查询并点击『提交运行』，我将为你分析具体问题。"

    # 写入 AI 回复
    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="assistant",
        content=reply,
    )
    await session.commit()

    return ChatSendOut(reply=reply)


@router.get("/submissions", response_model=list[SubmissionOut])
async def get_my_submissions(
    question_id: int | None = Query(None, description="题目 ID（可选，过滤特定题目）"),
    limit: int = Query(100, ge=1, le=200, description="限制数量"),
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """获取当前用户的提交记录。

    支持按题目 ID 过滤，返回按时间倒序排列的提交记录。
    """
    repo = SubmissionRepository(session)
    submissions = await repo.get_user_submissions(
        user_id, question_id, limit
    )
    return submissions


@router.get("/submissions/{submission_id}", response_model=SubmissionOut)
async def get_submission(
    submission_id: int,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """获取单条提交记录详情。

    只能查看自己的提交记录。
    """
    repo = SubmissionRepository(session)
    submission = await repo.get_by_id(submission_id)
    
    if not submission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"提交记录 ID {submission_id} 不存在"
        )
    
    # 验证是否是自己的提交记录
    if submission.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问此提交记录"
        )
    
    return submission


__all__ = ["router"]
