"""
AI 相关路由（/ai）

包含：
- `/ai/sql-hint`: 仅生成提示（不判题，便于调试）
- `/ai/check-sql`: 判题 + 提示（可选闭环：AST -> BKT -> lambda -> actions）
- `/ai/mastery-radar`: 获取学习画像（知识点掌握度），用于前端雷达图等可视化
- `/ai/chat/*`: 多轮对话历史与继续对话
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from core.ai_service import get_sql_hint, chat_with_teacher
from core.sql_judge import SQLJudgeService, SQLJudgeError, SQLSafetyError
from core.scaffolding import calculate_hint_level, get_ability_adjustment
from core.judge_setup import generate_init_sql_from_schema_preview, execute_setup_sql
from repository import QuestionRepository, SubmissionRepository, ChatRepository, UserRepository
from core.experience_service import compute_xp_gain, get_level_from_total
from schemas.submission import SubmissionCreate, SubmissionOut
from schemas.chat import ChatMessageOut, ChatSendIn, ChatSendOut
from dependencies import get_session
from core.auth import AuthHandler

# 新增的数学闭环核心引擎库
from core.ast_analyzer import compute_error_vector, infer_knowledge_points_from_sql
from core.bkt_service import update_mastery_from_errors, get_user_mastery_state
from core.control_strategy import ControlStrategy
from core.action_selector import ActionSelector
from core.error_attribution import evidence_weights_from_observation

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
    # 新增透传参数给前端（可选，用于在控制台或界面了解当前严厉程度变化）
    lambda_t: float | None = None
    # 阶段 1/2：可解释证据与知识点级错误归因，便于调试/科研记录
    observation: dict | None = None
    error_attributions: list[dict] = Field(default_factory=list)

@router.post("/sql-hint")
async def sql_hint(payload: SQLRequest):
    """获取 SQL 提示（不进行判题，仅用于测试）。"""
    try:
        hint = await get_sql_hint(payload.sql)
        return {"hint": hint}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"AI 服务调用失败: {str(e)}"
        )

@router.get("/mastery-radar")
async def get_mastery_radar(
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """取得当前学生的知识掌握度画像 (为了后续呈现雷达图)"""
    state_map = await get_user_mastery_state(session, user_id)
    return {"mastery_state": state_map}

@router.post("/check-sql", response_model=SQLCheckResponse)
async def check_sql(
    payload: SQLCheckRequest,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """检查学生提交的 SQL 是否正确，并生成 AI 教学提示。"""
    # 1. 查询题目
    question_repo = QuestionRepository(session)
    question = await question_repo.get_by_id(payload.question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {payload.question_id} 不存在",
        )

    # 1.5 判题前自动建表
    init_sql = generate_init_sql_from_schema_preview(getattr(question, "schema_preview", None))
    if init_sql:
        await execute_setup_sql(session, init_sql)

    # 2. SQL 判题
    judge_service = SQLJudgeService(session)
    is_correct = False
    error_message = None
    is_safety_blocked = False
    judge_detail = None
    attribution_result = None
    observation = None
    error_attributions: list[dict] = []

    try:
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

    submission_repo = SubmissionRepository(session)
    failure_count = await submission_repo.get_failure_count(user_id, payload.question_id)
    correct_count_before = await submission_repo.get_correct_count(user_id, payload.question_id)

    # ------------------------------------------------------------
    # Phase 1-4: 闭环数学控制流程介入
    # ------------------------------------------------------------

    # 安全拦截（如 DROP/DELETE）不应进入 AST/BKT/控制流程，避免污染学习画像
    if is_safety_blocked:
        structural_errors = []
        hint_actions = []
        lambda_t = None
        stats = await submission_repo.get_user_overall_stats(user_id)
        ability_adj = get_ability_adjustment(stats["success_rate"], stats["total"])
        hint_level = calculate_hint_level(failure_count, ability_adj)
    else:
        # 这里的闭环只处理“正常的 SELECT 尝试”：
        # 1) Observe + Diagnosis: E_AST / E_data / E_MUT -> KP attribution -> error_vector
        # 2) BKT: 以知识点维度更新 p_mastery -> current_mastery_state
        # 3) Control: 用成长 + 疲劳/挫折计算 lambda_t
        # 4) Action: 把最关键的 1~2 个缺陷转换为可执行的提示动作 HintAction[]
        #
        # 这些中间信息不会直接暴露给学生（只用于影响提示策略），但可通过 lambda_t 透传给前端用于调试/可视化。
        # 获取历史记录对象
        history_subs = await submission_repo.get_user_submissions(user_id, payload.question_id, limit=50)
        timestamps = [sub.created_at for sub in history_subs]
        if len(timestamps) == 0:
            timestamps.append(datetime.utcnow())  # 初次提交兜底

        session_duration_minutes = ControlStrategy.get_session_duration_minutes(timestamps)

        knowledge_points = infer_knowledge_points_from_sql(question.correct_sql)

        # P1/P2: 获取阶段 1/2 的可解释证据与知识点级归因
        attribution_result = evidence_weights_from_observation(
            student_sql=payload.student_sql,
            answer_sql=question.correct_sql,
            is_correct=is_correct,
            error_message=error_message,
            judge_detail=judge_detail,
        )
        structural_errors = attribution_result.to_ast_errors()
        observation = attribution_result.observation
        error_attributions = [item.to_dict() for item in attribution_result.attributions]

        # 如果归因器没有抓到明确证据，回退现有 AST+LLM 差异分析，保持旧链路可用。
        if not structural_errors and not is_correct:
            structural_errors = await compute_error_vector(
                student_sql=payload.student_sql,
                answer_sql=question.correct_sql
            )
        attribution_kps = [err.knowledge_point_id for err in structural_errors]
        knowledge_points = list(dict.fromkeys([*knowledge_points, *attribution_kps]))

        # P2: 保存上一次状态 L_{t-1}, 并更新当次掌握度的状态 P(L_n) -> L_t
        previous_mastery_state = await get_user_mastery_state(session, user_id)
        current_mastery_state = await update_mastery_from_errors(
            session=session,
            user_id=user_id,
            error_vector=structural_errors,
            question_knowledge_points=knowledge_points,
            overall_is_correct=is_correct
        )

        # 由于上面的写入是在 session 里还没 commit，为防止死锁直接拿字典
        if not current_mastery_state:
            current_mastery_state = previous_mastery_state

        # P3: 提取控制严厉系数 \lambda_t
        lambda_t = ControlStrategy.compute_lambda(
            current_mastery=current_mastery_state,
            previous_mastery=previous_mastery_state,
            session_duration_minutes=session_duration_minutes,
            consecutive_failures=failure_count
        )

        # P4: 搜索组装剧本 Actions
        hint_actions = ActionSelector.select_hint_actions(
            error_vector=structural_errors,
            mastery_state=current_mastery_state,
            lambda_t=lambda_t
        )

        hint_level = ControlStrategy.lambda_to_hint_level(lambda_t)  # 重新包装为 1, 2, 3

    # 对话计费统计
    chat_repo = ChatRepository(session)
    chat_count_for_xp = await chat_repo.count_messages_for_user_question(user_id, payload.question_id)

    # 5. 调用 AI 服务生成提示 (带入剧本和严厉度)
    try:
        ai_hint_result = await get_sql_hint(
            student_sql=payload.student_sql,
            question_content=question.content,
            is_correct=is_correct,
            hint_level=hint_level,
            failure_count=failure_count,
            error_message=error_message,
            language=payload.language,
            is_safety_blocked=is_safety_blocked,
            structural_errors=structural_errors, # 新参
            hint_actions=hint_actions,           # 新参
            lambda_t=lambda_t                    # 新参
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 服务暂时不可用，本次未计入提交次数，请稍后重试。",
        ) from e

    ai_hint_text = ai_hint_result.overall_comment

    # 6. 保存提交记录
    submission_data = SubmissionCreate(
        user_id=user_id,
        question_id=payload.question_id,
        student_sql=payload.student_sql,
        ai_hint=ai_hint_text,
        is_correct=is_correct,
        hint_level=hint_level,
    )
    submission = await submission_repo.create(submission_data)

    # 经验值发放结算
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

    # 最终提交事务（包含上面产生的 BKT 知识追踪更新、以及聊天记录更新）
    await session.commit()

    return SQLCheckResponse(
        is_correct=is_correct,
        hint=ai_hint_result.model_dump(),
        submission_id=submission.id,
        error_message=error_message,
        is_safety_blocked=is_safety_blocked,
        earned_experience=earned_experience,
        level_up=level_up,
        new_level=new_level,
        lambda_t=lambda_t,
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

    # 失败次数与用户整体表现用于支架等级
    failure_count = await submission_repo.get_failure_count(user_id, payload.question_id)
    stats = await submission_repo.get_user_overall_stats(user_id)
    ability_adj = get_ability_adjustment(stats["success_rate"], stats["total"])
    hint_level = calculate_hint_level(failure_count, ability_adj)

    # 历史对话（仅 user/assistant 参与模型上下文）
    chat_repo = ChatRepository(session)
    history_msgs = await chat_repo.list_messages(user_id=user_id, question_id=payload.question_id, limit=50)
    history_for_llm = [
        {"role": m.role, "content": m.content}
        for m in history_msgs
        if m.role in ("user", "assistant")
    ]

    # 先写入用户消息
    await chat_repo.add_message(
        user_id=user_id,
        question_id=payload.question_id,
        role="user",
        content=payload.message,
    )

    # 调用 AI 继续对话
    reply = await chat_with_teacher(
        question_content=question.content,
        latest_student_sql=latest.student_sql if latest else None,
        latest_is_correct=latest.is_correct if latest else None,
        latest_error_message=None,  # 这里不重复传错误，AI 已可从对话与提示理解
        hint_level=hint_level,
        failure_count=failure_count,
        history=history_for_llm,
        user_message=payload.message,
        language=payload.language,
    )

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


