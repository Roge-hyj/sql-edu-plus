from core.ai_question_generator import _normalize_generated_sql_dialect


def test_generated_question_unknown_dialect_falls_back_to_auto_mode():
    assert _normalize_generated_sql_dialect("snowflake") is None
    assert _normalize_generated_sql_dialect(None) is None
    assert _normalize_generated_sql_dialect("postgresql") == "postgres"
