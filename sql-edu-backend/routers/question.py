"""题目管理路由。"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

from repository import (
    QuestionRepository,
    QuestionSkillRepository,
    QuestionSkillSpec,
    SubmissionRepository,
    ChatRepository,
    DifficultyFeedbackRepository,
)
from schemas.question import (
    QuestionOut,
    QuestionPublicOut,
    QuestionCreate,
    QuestionSkillDeclaration,
    QuestionSkillOut,
    DifficultyFeedbackIn,
)
from schemas import ResponseOut
from dependencies import get_session, require_teacher
from core.auth import AuthHandler
from core.public_schema_preview import sanitize_schema_preview
from models.question import Question
from models.question_skill import QuestionSkillProvenance
from core.difficulty_service import compute_display_difficulty
from core.sql_knowledge_points import get_all_knowledge_points, get_knowledge_point_by_id
from core.ai_question_generator import (
    generate_questions_for_knowledge_point,
    infer_difficulty_from_question,
    infer_schema_preview_from_sql,
    infer_question_i18n_from_zh,
    infer_alias_requirement_from_content,
)
from core.sql_parser import infer_output_columns_from_sql

router = APIRouter(prefix="/questions", tags=["questions"])
auth_handler = AuthHandler()


def _declared_skill_specs(
    skills: list[QuestionSkillDeclaration] | None,
) -> list[QuestionSkillSpec]:
    specs: list[QuestionSkillSpec] = []
    for item in skills or []:
        if item.taxonomy_version is None:  # defensive: schema validator resolves it
            raise ValueError("taxonomy_version 未解析")
        specs.append(
            QuestionSkillSpec(
                skill_id=item.skill_id,
                taxonomy_version=item.taxonomy_version,
                role=item.role,
                observable_on_correct=bool(item.observable_on_correct),
            )
        )
    return specs


def _has_alias_requirement_in_content_quick(content: str | None) -> bool:
    """快速关键词检测：题目描述是否对列别名有「显式要求」。

    只有题干里明确提到「别名」「alias」等关键词时，才认为有别名要求。
    """
    if not content:
        return False
    text = str(content)
    # 中文提示词
    if "别名" in text:
        return True
    # 英文 alias 关键字（明确表示别名）
    lower = text.lower()
    if "alias" in lower:
        return True
    # 明确要求重命名列的表述
    if "rename" in lower and "column" in lower:
        return True
    if "name the column" in lower or "name it" in lower:
        return True
    return False


async def _has_alias_requirement_in_content(content: str | None) -> bool:
    """判断题目描述是否对列别名有「显式要求」。

    先用关键词快速检测，若无命中则调用 AI 进行语义判断。
    """
    if not content:
        return False
    # 快速关键词检测
    if _has_alias_requirement_in_content_quick(content):
        return True
    # 关键词未命中，调用 AI 进行语义判断
    try:
        return await infer_alias_requirement_from_content(content)
    except Exception:
        return False


async def _enrich_question_out(
    session: AsyncSession,
    question: Question,
) -> QuestionOut:
    """为题目附加动态难度。"""
    sub_repo = SubmissionRepository(session)
    chat_repo = ChatRepository(session)
    feedback_repo = DifficultyFeedbackRepository(session)
    skill_repo = QuestionSkillRepository(session)

    sub_stats = await sub_repo.get_question_submission_stats(question.id)
    chat_count = await chat_repo.count_messages_by_question(question.id)
    feedback_stats = await feedback_repo.get_question_stats(question.id)
    skill_rows = await skill_repo.list_by_question_id(question.id)

    disp = compute_display_difficulty(
        teacher_difficulty=question.difficulty,
        total_submissions=sub_stats["total_submissions"],
        correct_submissions=sub_stats["correct_submissions"],
        total_chat_messages=chat_count,
        feedback_count=feedback_stats["feedback_count"],
        avg_student_rating=feedback_stats.get("avg_rating"),
    )
    required_cols = getattr(question, "required_output_columns", None)
    # 不再从 SQL 推断填充，只返回数据库中实际存储的值（题目描述有别名要求时才会存储）

    return QuestionOut(
        id=question.id,
        title=question.title,
        content=question.content,
        title_en=getattr(question, "title_en", None),
        content_en=getattr(question, "content_en", None),
        title_zh_tw=getattr(question, "title_zh_tw", None),
        content_zh_tw=getattr(question, "content_zh_tw", None),
        difficulty=question.difficulty,
        correct_sql=question.correct_sql,
        sql_dialect=getattr(question, "sql_dialect", None),
        engine_version=getattr(question, "engine_version", None),
        schema_preview=getattr(question, "schema_preview", None),
        required_output_columns=required_cols,
        display_difficulty=disp,
        skills=[QuestionSkillOut.model_validate(row) for row in skill_rows],
    )


def _question_public_out(
    question: Question,
    *,
    display_difficulty: float,
) -> QuestionPublicOut:
    """Build the learner DTO without ever copying ``correct_sql``."""
    return QuestionPublicOut(
        id=question.id,
        title=question.title,
        content=question.content,
        title_en=getattr(question, "title_en", None),
        content_en=getattr(question, "content_en", None),
        title_zh_tw=getattr(question, "title_zh_tw", None),
        content_zh_tw=getattr(question, "content_zh_tw", None),
        difficulty=question.difficulty,
        sql_dialect=getattr(question, "sql_dialect", None),
        engine_version=getattr(question, "engine_version", None),
        schema_preview=sanitize_schema_preview(
            getattr(question, "schema_preview", None),
            forbidden_sql=getattr(question, "correct_sql", "") or "",
        ),
        required_output_columns=getattr(question, "required_output_columns", None),
        display_difficulty=display_difficulty,
    )


async def _enrich_public_question_out(
    session: AsyncSession,
    question: Question,
) -> QuestionPublicOut:
    """Add dynamic learner metadata while retaining the public-only shape."""
    sub_repo = SubmissionRepository(session)
    chat_repo = ChatRepository(session)
    feedback_repo = DifficultyFeedbackRepository(session)
    sub_stats = await sub_repo.get_question_submission_stats(question.id)
    chat_count = await chat_repo.count_messages_by_question(question.id)
    feedback_stats = await feedback_repo.get_question_stats(question.id)
    disp = compute_display_difficulty(
        teacher_difficulty=question.difficulty,
        total_submissions=sub_stats["total_submissions"],
        correct_submissions=sub_stats["correct_submissions"],
        total_chat_messages=chat_count,
        feedback_count=feedback_stats["feedback_count"],
        avg_student_rating=feedback_stats.get("avg_rating"),
    )
    return _question_public_out(question, display_difficulty=disp)


@router.get("/", response_model=list[QuestionPublicOut])
async def get_questions(
    skip: Annotated[int, Query(ge=0, description="跳过数量")] = 0,
    # 放宽单次最大返回数量上限，便于「无限加题」场景。
    # 默认 1000 条，最大可请求 10000 条。
    limit: Annotated[int, Query(ge=1, le=10000, description="限制数量")] = 1000,
    session: AsyncSession = Depends(get_session),
):
    """获取题目列表（分页）。返回动态难度。"""
    repo = QuestionRepository(session)
    questions = await repo.get_all(skip=skip, limit=limit)
    if not questions:
        return []

    qids = [q.id for q in questions]
    sub_repo = SubmissionRepository(session)
    chat_repo = ChatRepository(session)
    feedback_repo = DifficultyFeedbackRepository(session)

    sub_map = await sub_repo.get_submission_stats_by_question_ids(qids)
    chat_map = await chat_repo.count_messages_by_question_ids(qids)
    feedback_map = await feedback_repo.get_feedback_stats_by_question_ids(qids)

    out = []
    for q in questions:
        sub = sub_map.get(q.id, {"total_submissions": 0, "correct_submissions": 0})
        chat_count = chat_map.get(q.id, 0)
        fb = feedback_map.get(q.id, {"feedback_count": 0, "avg_rating": None})
        disp = compute_display_difficulty(
            q.difficulty,
            sub["total_submissions"],
            sub["correct_submissions"],
            chat_count,
            fb["feedback_count"],
            fb.get("avg_rating"),
        )
        # 不再从 SQL 推断填充，只返回数据库中实际存储的值
        out.append(
            _question_public_out(
                q,
                display_difficulty=disp,
            )
        )
    return out


@router.get("/knowledge-points")
async def get_knowledge_points(
    user_id: int = Depends(require_teacher),
):
    """获取 SQL 从入门到精通的知识点分类（教师端按知识点生成题目用）。"""
    return get_all_knowledge_points()


class GenerateByAIIn(BaseModel):
    knowledge_point_id: str
    count: int = 1  # 1～5


class InferOutputColumnsIn(BaseModel):
    correct_sql: str


@router.post("/infer-output-columns")
async def infer_output_columns(
    payload: InferOutputColumnsIn,
    user_id: int = Depends(require_teacher),
):
    """根据标准答案 SQL 自动解析要求的结果列名（教师端预览用）。留空保存时后端也会自动填入。"""
    result = infer_output_columns_from_sql(payload.correct_sql or "")
    return {"required_output_columns": result}


@router.post("/generate-by-ai", response_model=list[QuestionOut])
async def generate_questions_by_ai(
    payload: GenerateByAIIn,
    user_id: int = Depends(require_teacher),
    session: AsyncSession = Depends(get_session),
):
    """根据知识点由 AI 生成题目并加入题库（教师端）。"""
    point = get_knowledge_point_by_id(payload.knowledge_point_id)
    if not point:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"未知知识点：{payload.knowledge_point_id}",
        )
    count = max(1, min(5, payload.count))
    items = await generate_questions_for_knowledge_point(
        knowledge_point_id=payload.knowledge_point_id,
        count=count,
    )
    if not items:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI 未返回有效题目，请稍后重试或更换知识点",
        )
    created: list[Question] = []
    skill_repo = QuestionSkillRepository(session)
    try:
        for item in items:
            correct_sql = item["correct_sql"]
            content_str = (item.get("content") or "").strip()
            # 只有当题干里「明确提出别名要求」时，才根据标准答案 SQL 解析并启用列名校验
            needs_alias = await _has_alias_requirement_in_content(content_str)
            required_cols = (
                infer_output_columns_from_sql(correct_sql) if needs_alias else None
            )
            q = Question(
                title=item["title"],
                content=content_str,
                title_en=item.get("title_en"),
                content_en=item.get("content_en"),
                title_zh_tw=item.get("title_zh_tw"),
                content_zh_tw=item.get("content_zh_tw"),
                difficulty=max(1, min(10, item["difficulty"])),
                correct_sql=correct_sql,
                sql_dialect=item.get("sql_dialect"),
                engine_version=item.get("engine_version"),
                schema_preview=item.get("schema_preview"),
                required_output_columns=required_cols,
            )
            session.add(q)
            await session.flush()
            await skill_repo.add_ai_generated_primary(
                q.id,
                payload.knowledge_point_id,
            )
            await session.refresh(q)
            created.append(q)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    out = []
    for q in created:
        await session.refresh(q)
        out.append(await _enrich_question_out(session, q))
    return out


@router.post("/{question_id}/generate-schema-preview", response_model=QuestionOut)
async def generate_schema_preview(
    question_id: int,
    user_id: int = Depends(require_teacher),
    session: AsyncSession = Depends(get_session),
):
    """根据题目内容与标准答案 SQL，由 AI 生成表结构预览（供学生查看列名与示例数据）。"""
    repo = QuestionRepository(session)
    question = await repo.get_by_id(question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {question_id} 不存在",
        )
    preview = await infer_schema_preview_from_sql(question.content, question.correct_sql)
    if not preview:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI 未能生成表预览，请稍后重试",
        )
    from sqlalchemy import update
    stmt = (
        update(Question)
        .where(Question.id == question_id)
        .values(schema_preview=preview)
    )
    await session.execute(stmt)
    await session.commit()
    await session.refresh(question)
    return await _enrich_question_out(session, question)


@router.post("/{question_id}/generate-i18n", response_model=QuestionPublicOut)
async def generate_question_i18n(
    question_id: int,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """为题目生成英文/繁体题面（任何已登录用户均可触发；缺失时才生成）。"""
    from sqlalchemy import update

    repo = QuestionRepository(session)
    question = await repo.get_by_id(question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {question_id} 不存在",
        )

    # 若已有翻译就不重复生成（避免浪费额度）
    has_en = bool(getattr(question, "title_en", None)) and bool(getattr(question, "content_en", None))
    has_tw = bool(getattr(question, "title_zh_tw", None)) and bool(getattr(question, "content_zh_tw", None))
    if has_en and has_tw:
        return await _enrich_public_question_out(session, question)

    result = await infer_question_i18n_from_zh(question.title, question.content)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI 未能生成多语言题面，请稍后重试",
        )

    await session.execute(
        update(Question)
        .where(Question.id == question_id)
        .values(
            title_en=result["title_en"][:200],
            content_en=result["content_en"],
            title_zh_tw=result["title_zh_tw"][:200],
            content_zh_tw=result["content_zh_tw"],
        )
    )
    await session.commit()
    await session.refresh(question)
    return await _enrich_public_question_out(session, question)


@router.get("/{question_id}", response_model=QuestionPublicOut)
async def get_question(
    question_id: int,
    session: AsyncSession = Depends(get_session),
):
    """获取题目详情。返回动态难度；若本题尚无表结构预览则自动生成并落库。"""
    repo = QuestionRepository(session)
    question = await repo.get_by_id(question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {question_id} 不存在"
        )
    # 无表结构预览时自动生成并保存，确保每道题都有图表参考
    schema_preview = getattr(question, "schema_preview", None)
    if not (schema_preview and str(schema_preview).strip()):
        preview = await infer_schema_preview_from_sql(question.content, question.correct_sql)
        if preview:
            from sqlalchemy import update
            await session.execute(
                update(Question).where(Question.id == question_id).values(schema_preview=preview)
            )
            await session.commit()
            await session.refresh(question)
    return await _enrich_public_question_out(session, question)


@router.post("/", response_model=QuestionOut, status_code=status.HTTP_201_CREATED)
async def create_question(
    question_data: QuestionCreate,
    user_id: int = Depends(require_teacher),
    session: AsyncSession = Depends(get_session),
):
    """创建题目（需要教师权限）。难度留空时由 AI 根据题目内容与 SQL 自动判断；要求的结果列名留空时从标准答案 SQL 自动解析。"""
    difficulty = question_data.difficulty
    if difficulty is None:
        difficulty = await infer_difficulty_from_question(
            question_data.content, question_data.correct_sql
        )
    # 根据题目描述是否提到"别名要求"来决定要不要校验列名
    needs_alias = await _has_alias_requirement_in_content(question_data.content)
    required_cols = infer_output_columns_from_sql(question_data.correct_sql) if needs_alias else None
    try:
        question = Question(
            title=question_data.title,
            content=question_data.content,
            title_en=question_data.title_en,
            content_en=question_data.content_en,
            title_zh_tw=question_data.title_zh_tw,
            content_zh_tw=question_data.content_zh_tw,
            difficulty=difficulty,
            correct_sql=question_data.correct_sql,
            sql_dialect=question_data.sql_dialect,
            engine_version=question_data.engine_version,
            schema_preview=question_data.schema_preview,
            required_output_columns=required_cols,
        )
        session.add(question)
        await session.flush()
        if question_data.skills is not None:
            await QuestionSkillRepository(session).replace_for_question(
                question.id,
                _declared_skill_specs(question_data.skills),
                provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
            )
        await session.refresh(question)
        await session.commit()

        # 统一返回 QuestionOut，附带动态难度
        return await _enrich_question_out(session, question)
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建题目失败: {str(e)}"
        )


@router.put("/{question_id}", response_model=QuestionOut)
async def update_question(
    question_id: int,
    question_data: QuestionCreate,
    user_id: int = Depends(require_teacher),
    session: AsyncSession = Depends(get_session),
):
    """更新题目（需要教师权限）。难度留空时由 AI 根据题目内容与 SQL 自动判断；要求的结果列名留空时从标准答案 SQL 自动解析。"""
    from sqlalchemy import update
    repo = QuestionRepository(session)
    question = await repo.get_by_id(question_id)

    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {question_id} 不存在"
        )

    difficulty = question_data.difficulty
    if difficulty is None:
        difficulty = await infer_difficulty_from_question(
            question_data.content, question_data.correct_sql
        )
    needs_alias = await _has_alias_requirement_in_content(question_data.content)
    required_cols = infer_output_columns_from_sql(question_data.correct_sql) if needs_alias else None

    try:
        # 仅更新"明确传入"的可选字段，避免前端未传时覆盖成 None
        # title/content/correct_sql/difficulty 会始终更新（必填或可推断）
        values: dict = {
            "title": question_data.title,
            "content": question_data.content,
            "difficulty": difficulty,
            "correct_sql": question_data.correct_sql,
            "required_output_columns": required_cols,
        }
        fields_set = getattr(question_data, "model_fields_set", set())
        if "sql_dialect" in fields_set:
            values["sql_dialect"] = question_data.sql_dialect
        if "schema_preview" in fields_set:
            values["schema_preview"] = question_data.schema_preview
        if "engine_version" in fields_set:
            values["engine_version"] = question_data.engine_version
        if "title_en" in fields_set:
            values["title_en"] = question_data.title_en
        if "content_en" in fields_set:
            values["content_en"] = question_data.content_en
        if "title_zh_tw" in fields_set:
            values["title_zh_tw"] = question_data.title_zh_tw
        if "content_zh_tw" in fields_set:
            values["content_zh_tw"] = question_data.content_zh_tw

        stmt = update(Question).where(Question.id == question_id).values(**values)
        await session.execute(stmt)
        if "skills" in fields_set and question_data.skills is not None:
            await QuestionSkillRepository(session).replace_for_question(
                question_id,
                _declared_skill_specs(question_data.skills),
                provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
            )
        await session.commit()
        await session.refresh(question)

        # 统一返回 QuestionOut，附带动态难度
        return await _enrich_question_out(session, question)
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新题目失败: {str(e)}"
        )


@router.delete("/{question_id}", response_model=ResponseOut)
async def delete_question(
    question_id: int,
    user_id: int = Depends(require_teacher),
    session: AsyncSession = Depends(get_session),
):
    """删除题目（需要教师权限）。
    
    注意：删除题目会级联删除所有相关的提交记录（CASCADE）。
    """
    from sqlalchemy import delete
    repo = QuestionRepository(session)
    question = await repo.get_by_id(question_id)
    
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {question_id} 不存在"
        )
    
    try:
        # 删除题目（级联删除相关提交记录）
        stmt = delete(Question).where(Question.id == question_id)
        await session.execute(stmt)
        await session.commit()

        return ResponseOut(result="success", detail="题目删除成功")
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除题目失败: {str(e)}"
        )


@router.post("/{question_id}/difficulty-feedback", response_model=ResponseOut)
async def submit_difficulty_feedback(
    question_id: int,
    payload: DifficultyFeedbackIn,
    user_id: int = Depends(auth_handler.auth_access_dependency),
    session: AsyncSession = Depends(get_session),
):
    """学生正确完成该题后可提交难度评分（1～10）。需认证，且该用户在该题上至少有一次正确提交。"""
    if payload.rating < 1 or payload.rating > 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="难度评分为 1～10 的整数",
        )
    repo = QuestionRepository(session)
    question = await repo.get_by_id(question_id)
    if not question:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"题目 ID {question_id} 不存在",
        )
    sub_repo = SubmissionRepository(session)
    subs = await sub_repo.get_user_submissions(user_id, question_id, limit=1)
    correct_any = any(s.is_correct for s in subs) if subs else False
    if not correct_any:
        # 检查该用户在该题是否有任意正确记录（可能不在最近一条）
        all_subs = await sub_repo.get_user_submissions(user_id, question_id, limit=500)
        correct_any = any(s.is_correct for s in all_subs)
    if not correct_any:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="请先正确完成本题后再提交难度评分",
        )
    feedback_repo = DifficultyFeedbackRepository(session)
    await feedback_repo.add(user_id, question_id, payload.rating)
    await session.commit()
    return ResponseOut(result="success", detail="难度评分已记录")
