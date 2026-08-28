import json

import pytest
from pydantic import ValidationError

from models.question import Question
from routers.question import _question_public_out, router
from schemas.question import QuestionCreate, QuestionOut, QuestionPublicOut


def _question_payload(**overrides):
    payload = {
        "title": "Portable query",
        "content": "List all users",
        "correct_sql": "SELECT id FROM users",
    }
    payload.update(overrides)
    return payload


def test_question_create_uses_null_for_unspecified_or_blank_dialect():
    assert QuestionCreate(**_question_payload()).sql_dialect is None
    assert QuestionCreate(**_question_payload(sql_dialect=" ")).sql_dialect is None


def test_question_create_normalizes_dialect_alias():
    assert QuestionCreate(**_question_payload(sql_dialect="PostgreSQL")).sql_dialect == "postgres"
    assert QuestionCreate(**_question_payload(sql_dialect="SQLServer")).sql_dialect == "tsql"
    assert QuestionCreate(**_question_payload(sql_dialect="ANSI")).sql_dialect == "standard"


def test_question_create_rejects_unknown_dialect():
    with pytest.raises(ValidationError):
        QuestionCreate(**_question_payload(sql_dialect="snowflake"))


def test_question_create_rejects_removed_timed_challenge_field():
    with pytest.raises(ValidationError):
        QuestionCreate(**_question_payload(time_limit_seconds=120))


def test_question_output_allows_null_dialect():
    output = QuestionOut(
        id=1,
        title="Portable query",
        content="List all users",
        difficulty=1,
        correct_sql="SELECT id FROM users",
        sql_dialect=None,
    )

    assert output.sql_dialect is None


def test_public_question_schema_has_no_reference_sql_field():
    assert "correct_sql" not in QuestionPublicOut.model_fields
    public = QuestionPublicOut(
        id=1,
        title="Portable query",
        content="List all users",
        difficulty=1,
    )
    assert "correct_sql" not in public.model_dump()


def test_public_question_constructor_never_copies_reference_sql():
    question = Question(
        id=7,
        title="Portable query",
        content="List all users",
        difficulty=2,
        correct_sql="SELECT secret_answer FROM private_table",
    )
    public = _question_public_out(
        question,
        display_difficulty=2.5,
    )
    serialized = public.model_dump_json()
    assert "correct_sql" not in serialized
    assert question.correct_sql not in serialized


def test_public_question_sanitizes_llm_schema_preview_recursively():
    reference_sql = "SELECT secret_answer FROM private_table WHERE score > 10"
    question = Question(
        id=8,
        title="Portable query",
        content="List public values",
        difficulty=2,
        correct_sql=reference_sql,
        schema_preview=json.dumps(
            {
                "correct_sql": reference_sql,
                "explanation": "hidden chain of thought",
                "tables": [
                    {
                        "name": "private_table",
                        "columns": [
                            "id",
                            {"name": "score", "type": "INTEGER"},
                            {"name": "note", "type": "AVG(salary)"},
                            "WHERE credits > 3",
                        ],
                        "rows": [
                            {
                                "id": 1,
                                "score": 11,
                                "note": reference_sql,
                                "correct_sql": reference_sql,
                            },
                            {"id": 2, "score": 9, "note": "WHERE score > 10"},
                            {"id": 3, "score": 12, "note": "COUNT(DISTINCT id)"},
                            {"id": 4, "score": 10, "note": "boundary"},
                        ],
                        "answer_sql": reference_sql,
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )
    public = _question_public_out(
        question,
        display_difficulty=2.5,
    )
    preview = json.loads(public.schema_preview or "{}")
    encoded = json.dumps(preview, ensure_ascii=False)

    assert set(preview) == {"tables"}
    assert set(preview["tables"][0]) == {"name", "columns", "rows"}
    assert [item["name"] for item in preview["tables"][0]["columns"]] == [
        "id",
        "score",
        "note",
    ]
    assert preview["tables"][0]["columns"][1]["data_type"] == "INTEGER"
    assert "data_type" not in preview["tables"][0]["columns"][2]
    assert set(preview["tables"][0]["rows"][0]) == {"id", "score", "note"}
    assert preview["tables"][0]["rows"][0]["note"] == "[text]"
    assert preview["tables"][0]["rows"][1]["note"] == "[text]"
    assert preview["tables"][0]["rows"][2]["note"] == "[text]"
    assert preview["tables"][0]["rows"][3]["score"] == "[number]"
    assert reference_sql not in encoded
    assert "correct_sql" not in encoded
    assert "answer_sql" not in encoded
    assert "explanation" not in encoded


def test_public_schema_preview_keeps_only_resolvable_foreign_keys():
    question = Question(
        id=9,
        title="Schema relationships",
        content="Inspect the declared relationship",
        difficulty=2,
        correct_sql="SELECT id FROM parent",
        schema_preview=json.dumps(
            {
                "tables": [
                    {
                        "name": "parent",
                        "columns": ["id"],
                        "primary_key": ["id"],
                    },
                    {
                        "name": "child",
                        "columns": ["parent_id", "orphan_id"],
                        "foreign_keys": [
                            {
                                "column": "parent_id",
                                "references_table": "parent",
                                "references_column": "id",
                            },
                            {
                                "column": "orphan_id",
                                "references_table": "missing",
                                "references_column": "id",
                            },
                        ],
                    },
                ]
            }
        ),
    )

    public = _question_public_out(
        question,
        display_difficulty=2.0,
    )
    preview = json.loads(public.schema_preview or "{}")
    child = next(table for table in preview["tables"] if table["name"] == "child")

    assert child["foreign_keys"] == [
        {
            "columns": ["parent_id"],
            "references_table": "parent",
            "references_columns": ["id"],
        }
    ]


def test_public_get_routes_use_answer_free_response_models():
    routes = {
        (route.path, frozenset(route.methods or ())): route
        for route in router.routes
    }
    listing = routes[("/questions/", frozenset({"GET"}))]
    detail = routes[("/questions/{question_id}", frozenset({"GET"}))]

    assert listing.response_model == list[QuestionPublicOut]
    assert detail.response_model is QuestionPublicOut


def test_teacher_write_routes_keep_answer_bearing_response_model():
    route_models = {
        (route.path, method): route.response_model
        for route in router.routes
        for method in (route.methods or ())
    }
    assert route_models[("/questions/", "POST")] is QuestionOut
    assert route_models[("/questions/{question_id}", "PUT")] is QuestionOut
    assert route_models[("/questions/generate-by-ai", "POST")] == list[QuestionOut]
    assert (
        route_models[("/questions/{question_id}/generate-schema-preview", "POST")]
        is QuestionOut
    )


def test_student_accessible_i18n_generation_is_also_answer_free():
    route = next(
        item
        for item in router.routes
        if item.path == "/questions/{question_id}/generate-i18n"
    )
    assert route.response_model is QuestionPublicOut


def test_question_orm_column_is_nullable_without_mysql_default():
    column = Question.__table__.c.sql_dialect

    assert column.nullable is True
    assert column.default is None
    assert column.server_default is None


def test_question_update_payload_can_distinguish_omitted_from_explicit_null():
    omitted = QuestionCreate(**_question_payload())
    explicit_null = QuestionCreate(**_question_payload(sql_dialect=None))

    assert "sql_dialect" not in omitted.model_fields_set
    assert "sql_dialect" in explicit_null.model_fields_set
