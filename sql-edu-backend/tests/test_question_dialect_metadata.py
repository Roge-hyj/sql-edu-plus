import pytest
from pydantic import ValidationError

from models.question import Question
from schemas.question import QuestionCreate, QuestionOut


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
