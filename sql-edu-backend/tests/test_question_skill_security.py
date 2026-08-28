"""Security boundaries around the authoritative Question-Skill Q-matrix."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from core.auth import AuthHandler
from core.phase3_skill_catalog import ATOMIC_SKILL_TAXONOMY_VERSION
from dependencies import get_session
from main import app
from models.question import Question
from models.question_skill import (
    SQL_KNOWLEDGE_TAXONOMY_VERSION,
    QuestionSkill,
    QuestionSkillProvenance,
)
from repository.question_skill_repo import (
    QuestionSkillRepository,
    QuestionSkillSpec,
)
from routers import question as question_router
from schemas.question import QuestionCreate


@pytest.fixture
async def qmatrix_api_client(test_db_session):
    """Route all application DB dependencies to the isolated test transaction."""

    async def override_session():
        yield test_db_session

    marker = object()
    previous = app.dependency_overrides.get(get_session, marker)
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        if previous is marker:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous


def _write_payload(*, title: str, skills: list[dict[str, object]]) -> dict[str, object]:
    return {
        "title": title,
        "content": "查询学分严格超过 3 的课程",
        "correct_sql": "SELECT title FROM course WHERE credits > 3",
        "difficulty": 2,
        "skills": skills,
    }


@pytest.mark.asyncio
async def test_student_token_cannot_create_or_replace_qmatrix(
    test_db_session,
    test_user,
    test_question,
    qmatrix_api_client,
):
    repo = QuestionSkillRepository(test_db_session)
    await repo.replace_for_question(
        test_question.id,
        [QuestionSkillSpec(skill_id="where")],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    await test_db_session.commit()
    original_title = test_question.title
    original_count = await test_db_session.scalar(
        select(func.count(Question.id))
    )

    token = AuthHandler().encode_login_token(test_user.id)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    create_response = await qmatrix_api_client.post(
        "/questions/",
        headers=headers,
        json=_write_payload(
            title="Unauthorized create",
            skills=[{"skill_id": "filter.boundary"}],
        ),
    )
    update_response = await qmatrix_api_client.put(
        f"/questions/{test_question.id}",
        headers=headers,
        json=_write_payload(title="Unauthorized update", skills=[]),
    )

    assert create_response.status_code == 403
    assert update_response.status_code == 403
    assert await test_db_session.scalar(select(func.count(Question.id))) == original_count
    await test_db_session.refresh(test_question)
    assert test_question.title == original_title
    rows = await repo.list_by_question_id(test_question.id)
    assert [(row.skill_id, row.taxonomy_version) for row in rows] == [
        ("where", SQL_KNOWLEDGE_TAXONOMY_VERSION)
    ]


@pytest.mark.asyncio
async def test_public_routes_never_serialize_loaded_qmatrix(
    test_db_session,
    test_question,
    qmatrix_api_client,
):
    test_question.schema_preview = json.dumps({"tables": []})
    await QuestionSkillRepository(test_db_session).replace_for_question(
        test_question.id,
        [
            QuestionSkillSpec(
                skill_id="filter.boundary",
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
            )
        ],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    await test_db_session.commit()
    await test_db_session.refresh(test_question, attribute_names=["skill_mappings"])
    assert test_question.skill_mappings

    listing = await qmatrix_api_client.get("/questions/?limit=1000")
    detail = await qmatrix_api_client.get(f"/questions/{test_question.id}")

    assert listing.status_code == 200
    assert detail.status_code == 200
    encoded = json.dumps(
        {"listing": listing.json(), "detail": detail.json()},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "correct_sql" not in encoded
    assert test_question.correct_sql not in encoded
    assert "skills" not in encoded
    assert "skill_mappings" not in encoded
    assert "filter.boundary" not in encoded
    assert ATOMIC_SKILL_TAXONOMY_VERSION not in encoded
    assert "AUTHOR_DECLARED" not in encoded


@pytest.mark.asyncio
async def test_update_and_qmatrix_replacement_roll_back_together(
    test_db_session,
    test_question,
    monkeypatch,
):
    repo = QuestionSkillRepository(test_db_session)
    await repo.replace_for_question(
        test_question.id,
        [QuestionSkillSpec(skill_id="where")],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    await test_db_session.commit()
    original_title = test_question.title
    original_sql = test_question.correct_sql

    async def no_alias(_: str | None) -> bool:
        return False

    async def fail_replace(*_: object, **__: object) -> list[QuestionSkill]:
        raise RuntimeError("injected Q-matrix update failure")

    monkeypatch.setattr(
        question_router,
        "_has_alias_requirement_in_content",
        no_alias,
    )
    monkeypatch.setattr(
        QuestionSkillRepository,
        "replace_for_question",
        fail_replace,
    )

    with pytest.raises(HTTPException) as error:
        await question_router.update_question(
            test_question.id,
            QuestionCreate(
                **_write_payload(
                    title="Must roll back",
                    skills=[{"skill_id": "filter.boundary"}],
                )
            ),
            user_id=1,
            session=test_db_session,
        )

    assert error.value.status_code == 500
    await test_db_session.refresh(test_question)
    assert test_question.title == original_title
    assert test_question.correct_sql == original_sql
    rows = list(
        (
            await test_db_session.scalars(
                select(QuestionSkill)
                .where(QuestionSkill.question_id == test_question.id)
                .order_by(QuestionSkill.id)
            )
        ).all()
    )
    assert [(row.skill_id, row.taxonomy_version) for row in rows] == [
        ("where", SQL_KNOWLEDGE_TAXONOMY_VERSION)
    ]
