"""Question-Skill Q-matrix contract and persistence tests."""

import importlib.util
import io
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from models.question import Question
from models import Base
from core.phase3_skill_catalog import ATOMIC_SKILL_TAXONOMY_VERSION
from models.question_skill import (
    SQL_KNOWLEDGE_TAXONOMY_VERSION,
    QuestionSkill,
    QuestionSkillProvenance,
    QuestionSkillRole,
)
from repository.question_skill_repo import (
    QuestionSkillRepository,
    QuestionSkillSpec,
)
from routers import question as question_router
from schemas.question import QuestionCreate, QuestionPublicOut


def _payload(**overrides) -> QuestionCreate:
    raw = {
        "title": "Boundary exercise",
        "content": "查询学分严格超过 3 的课程",
        "correct_sql": "SELECT title FROM course WHERE credits > 3",
        "difficulty": 2,
    }
    raw.update(overrides)
    return QuestionCreate(**raw)


async def _no_alias(_: str | None) -> bool:
    return False


def test_skill_declaration_defaults_are_role_aware_and_versioned():
    payload = _payload(
        skills=[
            {"skill_id": "where"},
            {"skill_id": "select-basic", "role": "SUPPORTING"},
        ]
    )

    assert payload.skills is not None
    assert payload.skills[0].taxonomy_version == SQL_KNOWLEDGE_TAXONOMY_VERSION
    assert payload.skills[0].observable_on_correct is True
    assert payload.skills[1].observable_on_correct is False


def test_atomic_skill_resolves_only_to_explicit_atomic_taxonomy():
    payload = _payload(skills=[{"skill_id": "filter.boundary"}])

    assert payload.skills is not None
    assert (
        payload.skills[0].taxonomy_version
        == ATOMIC_SKILL_TAXONOMY_VERSION
    )

    explicitly_wrong = {
        "skill_id": "filter.boundary",
        "taxonomy_version": SQL_KNOWLEDGE_TAXONOMY_VERSION,
    }
    with pytest.raises(ValidationError):
        _payload(skills=[explicitly_wrong])


@pytest.mark.parametrize(
    "skill",
    [
        {"skill_id": "Where"},
        {"skill_id": "where/sql"},
        {"skill_id": "not-in-current-taxonomy"},
        {"skill_id": "where", "taxonomy_version": "future.v2"},
        {
            "skill_id": "where",
            "role": "SUPPORTING",
            "observable_on_correct": True,
        },
        {
            "skill_id": "where",
            "provenance": "AI_GENERATED",
        },
    ],
)
def test_skill_declaration_fails_closed_for_invalid_or_client_owned_facts(skill):
    with pytest.raises(ValidationError):
        _payload(skills=[skill])


def test_skill_list_is_bounded_deduplicated_and_limits_primary_targets():
    duplicate = _payload(skills=[{"skill_id": "where"}, {"skill_id": "where"}])
    assert duplicate.skills is not None
    assert len(duplicate.skills) == 1

    with pytest.raises(ValidationError):
        _payload(
            skills=[
                {"skill_id": "where"},
                {"skill_id": "select-basic"},
                {"skill_id": "order-by"},
                {"skill_id": "distinct"},
            ]
        )

    with pytest.raises(ValidationError):
        _payload(
            skills=[
                {"skill_id": skill_id, "role": "SUPPORTING"}
                for skill_id in (
                    "where",
                    "select-basic",
                    "order-by",
                    "distinct",
                    "alias",
                    "arithmetic",
                    "agg-count",
                    "group-by",
                    "having",
                )
            ]
        )


def test_conflicting_duplicate_skill_declarations_are_rejected():
    with pytest.raises(ValidationError):
        _payload(
            skills=[
                {"skill_id": "where", "observable_on_correct": True},
                {"skill_id": "where", "observable_on_correct": False},
            ]
        )


def test_database_contract_has_unique_cascade_and_supporting_check():
    table = QuestionSkill.__table__
    fk = next(iter(table.c.question_id.foreign_keys))
    constraint_names = {constraint.name for constraint in table.constraints}

    assert fk.ondelete == "CASCADE"
    assert "uq_question_skills_question_skill_taxonomy" in constraint_names
    assert "ck_question_skills_supporting_not_observable" in constraint_names
    assert "skill_role" in table.c
    assert "role" not in table.c


def test_mysql_migration_ddl_matches_orm_constraint_names_and_avoids_role_keyword():
    migration_path = (
        Path(__file__).parents[1]
        / "alembic/versions/d3e4f5a6b7c8_add_question_skill_qmatrix.py"
    )
    spec = importlib.util.spec_from_file_location(
        "question_skill_qmatrix_migration",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="mysql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    migration.op = Operations(context)
    migration.upgrade()
    ddl = output.getvalue()

    assert migration.down_revision == "c2d3e4f5a6b7"
    assert "skill_role VARCHAR(16) NOT NULL" in ddl
    assert "CONSTRAINT ck_question_skills_role_allowed CHECK" in ddl
    assert "ck_question_skills_ck_question_skills" not in ddl
    assert "skill_role = 'PRIMARY' OR NOT observable_on_correct" in ddl


@pytest.mark.asyncio
async def test_legacy_question_stays_unmapped_and_reference_sql_is_not_inferred(
    test_db_session,
    test_question,
):
    repo = QuestionSkillRepository(test_db_session)

    assert "WHERE age > 18" in test_question.correct_sql
    assert await repo.list_by_question_id(test_question.id) == []


@pytest.mark.asyncio
async def test_repository_replaces_rows_without_committing(test_db_session, test_question):
    repo = QuestionSkillRepository(test_db_session)
    await repo.replace_for_question(
        test_question.id,
        [
            QuestionSkillSpec(
                skill_id="where",
                role=QuestionSkillRole.PRIMARY,
                observable_on_correct=True,
            ),
            QuestionSkillSpec(
                skill_id="select-basic",
                role=QuestionSkillRole.SUPPORTING,
                observable_on_correct=False,
            ),
        ],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )

    rows = await repo.list_by_question_id(test_question.id)
    assert [row.skill_id for row in rows] == ["where", "select-basic"]
    assert {row.provenance for row in rows} == {"AUTHOR_DECLARED"}

    await test_db_session.rollback()
    assert await repo.list_by_question_id(test_question.id) == []


@pytest.mark.asyncio
async def test_teacher_create_persists_author_declared_mapping_in_same_transaction(
    test_db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        question_router,
        "_has_alias_requirement_in_content",
        _no_alias,
    )
    result = await question_router.create_question(
        _payload(
            skills=[
                {"skill_id": "where"},
                {"skill_id": "select-basic", "role": "SUPPORTING"},
            ]
        ),
        user_id=1,
        session=test_db_session,
    )

    assert [item.skill_id for item in result.skills] == ["where", "select-basic"]
    assert {item.provenance for item in result.skills} == {
        QuestionSkillProvenance.AUTHOR_DECLARED
    }


@pytest.mark.asyncio
async def test_teacher_can_declare_frozen_atomic_skill_without_sql_inference(
    test_db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        question_router,
        "_has_alias_requirement_in_content",
        _no_alias,
    )
    result = await question_router.create_question(
        _payload(skills=[{"skill_id": "filter.boundary"}]),
        user_id=1,
        session=test_db_session,
    )

    assert len(result.skills) == 1
    assert result.skills[0].skill_id == "filter.boundary"
    assert (
        result.skills[0].taxonomy_version
        == ATOMIC_SKILL_TAXONOMY_VERSION
    )


@pytest.mark.asyncio
async def test_teacher_update_omission_preserves_and_empty_list_clears(
    test_db_session,
    test_question,
    monkeypatch,
):
    monkeypatch.setattr(
        question_router,
        "_has_alias_requirement_in_content",
        _no_alias,
    )
    repo = QuestionSkillRepository(test_db_session)
    await repo.replace_for_question(
        test_question.id,
        [QuestionSkillSpec(skill_id="where")],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    await test_db_session.commit()

    preserved = await question_router.update_question(
        test_question.id,
        _payload(title="Updated without skills"),
        user_id=1,
        session=test_db_session,
    )
    assert [item.skill_id for item in preserved.skills] == ["where"]

    cleared = await question_router.update_question(
        test_question.id,
        _payload(title="Updated with empty skills", skills=[]),
        user_id=1,
        session=test_db_session,
    )
    assert cleared.skills == []
    assert await repo.list_by_question_id(test_question.id) == []


@pytest.mark.asyncio
async def test_ai_generation_persists_requested_skill_with_server_provenance(
    test_db_session,
    monkeypatch,
):
    async def generated_questions(**_: object) -> list[dict[str, object]]:
        return [
            {
                "title": "WHERE exercise",
                "content": "筛选行",
                "correct_sql": "SELECT id FROM users WHERE active = 1",
                "difficulty": 2,
                "sql_dialect": None,
                "engine_version": None,
                "schema_preview": None,
            }
        ]

    monkeypatch.setattr(
        question_router,
        "generate_questions_for_knowledge_point",
        generated_questions,
    )
    monkeypatch.setattr(
        question_router,
        "_has_alias_requirement_in_content",
        _no_alias,
    )

    result = await question_router.generate_questions_by_ai(
        question_router.GenerateByAIIn(knowledge_point_id="where", count=1),
        user_id=1,
        session=test_db_session,
    )

    assert len(result) == 1
    assert len(result[0].skills) == 1
    assert result[0].skills[0].skill_id == "where"
    assert result[0].skills[0].role is QuestionSkillRole.PRIMARY
    assert result[0].skills[0].observable_on_correct is True
    assert (
        result[0].skills[0].provenance
        is QuestionSkillProvenance.AI_GENERATED
    )


@pytest.mark.asyncio
async def test_ai_batch_rolls_back_questions_and_mappings_together(
    test_db_session,
    monkeypatch,
):
    async def generated_questions(**_: object) -> list[dict[str, object]]:
        return [
            {
                "title": f"Generated {index}",
                "content": "筛选行",
                "correct_sql": "SELECT id FROM users WHERE active = 1",
                "difficulty": 2,
            }
            for index in range(2)
        ]

    original = QuestionSkillRepository.add_ai_generated_primary
    calls = 0

    async def fail_second_mapping(
        self: QuestionSkillRepository,
        question_id: int,
        skill_id: str,
        **kwargs: object,
    ) -> QuestionSkill:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second mapping failure")
        return await original(
            self,
            question_id,
            skill_id,
            **kwargs,
        )

    monkeypatch.setattr(
        question_router,
        "generate_questions_for_knowledge_point",
        generated_questions,
    )
    monkeypatch.setattr(
        question_router,
        "_has_alias_requirement_in_content",
        _no_alias,
    )
    monkeypatch.setattr(
        QuestionSkillRepository,
        "add_ai_generated_primary",
        fail_second_mapping,
    )

    with pytest.raises(RuntimeError, match="second mapping failure"):
        await question_router.generate_questions_by_ai(
            question_router.GenerateByAIIn(knowledge_point_id="where", count=2),
            user_id=1,
            session=test_db_session,
        )

    generated = await test_db_session.scalars(
        select(Question).where(Question.title.like("Generated %"))
    )
    assert list(generated.all()) == []


@pytest.mark.asyncio
async def test_question_and_mapping_roll_back_together_on_mapping_failure(
    test_db_session,
    monkeypatch,
):
    monkeypatch.setattr(
        question_router,
        "_has_alias_requirement_in_content",
        _no_alias,
    )

    async def fail_replace(*_: object, **__: object) -> list[QuestionSkill]:
        raise RuntimeError("injected Q-matrix failure")

    monkeypatch.setattr(
        QuestionSkillRepository,
        "replace_for_question",
        fail_replace,
    )

    with pytest.raises(HTTPException) as error:
        await question_router.create_question(
            _payload(skills=[{"skill_id": "where"}]),
            user_id=1,
            session=test_db_session,
        )
    assert error.value.status_code == 500

    created = await test_db_session.scalar(
        select(Question).where(Question.title == "Boundary exercise")
    )
    assert created is None


def test_student_public_dto_does_not_expose_qmatrix():
    assert "skills" not in QuestionPublicOut.model_fields
    public = QuestionPublicOut(
        id=1,
        title="Public",
        content="Public content",
        difficulty=1,
    )
    assert "skills" not in public.model_dump()
