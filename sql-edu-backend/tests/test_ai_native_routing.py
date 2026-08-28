from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import core.parseval_data_generator as parseval
import routers.ai as ai_router
from routers.ai import SQLCheckRequest, check_sql


TEST_ATTEMPT_ID = "00000000-0000-4000-8000-000000000002"


@pytest.mark.parametrize(
    ("dialect", "setting_name", "url"),
    [
        ("mysql", "PARSEVAL_MYSQL_URL", "mysql://judge@mysql/parseval"),
        ("postgres", "PARSEVAL_POSTGRES_URL", "postgresql://judge@postgres/parseval"),
        ("tsql", "PARSEVAL_TSQL_URL", "mssql://judge@sqlserver/parseval"),
        ("oracle", "PARSEVAL_ORACLE_URL", "oracle://judge@oracle/parseval"),
    ],
)
def test_native_executor_url_uses_resolved_dialect(
    monkeypatch,
    dialect,
    setting_name,
    url,
):
    monkeypatch.setattr(ai_router.settings, setting_name, f"  {url}  ")

    assert ai_router._native_executor_url_for_dialect(dialect) == url


def test_native_executor_url_rejects_unknown_or_empty_settings(monkeypatch):
    monkeypatch.setattr(ai_router.settings, "PARSEVAL_POSTGRES_URL", "  ")

    assert ai_router._native_executor_url_for_dialect("postgres") is None
    assert ai_router._native_executor_url_for_dialect("sqlite") is None
    assert ai_router._native_executor_url_for_dialect(None) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'drop' AS word",
        "SELECT note FROM t WHERE note = 'insert'",
        "SELECT [update] FROM t",
    ],
)
def test_route_safety_precheck_ignores_keywords_in_literals_and_identifiers(sql):
    assert ai_router._student_sql_safety_error(sql, None) is None


def test_route_safety_precheck_rejects_actual_non_query_statement():
    error = ai_router._student_sql_safety_error("DROP TABLE users", None)

    assert error is not None
    assert error.code == "NATIVE_SQL_UNSAFE_STATEMENT"


def test_engine_version_contract_accepts_major_compatible_runner(monkeypatch):
    monkeypatch.setattr(ai_router.settings, "PARSEVAL_POSTGRES_VERSION", "16.10")

    ai_router._validate_native_engine_version("postgres", "16")
    ai_router._validate_native_engine_version("postgres", None)


def test_engine_version_contract_accepts_same_numeric_major(monkeypatch):
    monkeypatch.setattr(ai_router.settings, "PARSEVAL_MYSQL_VERSION", "8.0.46")

    ai_router._validate_native_engine_version("mysql", "8.0")


def test_engine_version_contract_rejects_wrong_or_unknown_runner(monkeypatch):
    monkeypatch.setattr(ai_router.settings, "PARSEVAL_POSTGRES_VERSION", "16")

    with pytest.raises(HTTPException) as caught:
        ai_router._validate_native_engine_version("postgres", "14")

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "ENGINE_VERSION_UNAVAILABLE"


class _ReadOnlyQuestionRepository:
    def __init__(self, _session, question):
        self.question = question

    async def get_by_id(self, _question_id):
        return self.question


class _TrackingSubmissionRepository:
    created = 0

    def __init__(self, _session):
        pass

    async def get_by_attempt_id(self, *_args, **_kwargs):
        return None

    async def get_failure_count(
        self,
        _user_id,
        _question_id,
        *,
        for_update=False,
    ):
        return 0

    async def get_correct_count(
        self,
        _user_id,
        _question_id,
        *,
        for_update=False,
    ):
        return 0

    async def create(self, _submission_data):
        type(self).created += 1
        raise AssertionError("platform failures must not create submissions")


class _NoWriteSession:
    commits = 0

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_dialect_conflict_returns_422_before_learning_state_access(monkeypatch):
    question = SimpleNamespace(
        id=19,
        correct_sql="SELECT `id` FROM users",
        sql_dialect=None,
    )
    session = _NoWriteSession()
    _TrackingSubmissionRepository.created = 0

    monkeypatch.setattr(
        ai_router,
        "QuestionRepository",
        lambda current_session: _ReadOnlyQuestionRepository(current_session, question),
    )

    def reject_learning_repository(_session):
        raise AssertionError("dialect conflicts must return before learning-state repositories")

    # The attempt-id idempotency gate performs a read-only submission lookup
    # before dialect resolution.  That read is allowed; writes are not.
    monkeypatch.setattr(ai_router, "SubmissionRepository", _TrackingSubmissionRepository)
    monkeypatch.setattr(ai_router, "ChatRepository", reject_learning_repository)

    with pytest.raises(HTTPException) as caught:
        await check_sql(
            payload=SQLCheckRequest(
                student_sql="SELECT id::INT FROM users",
                question_id=question.id,
                attempt_id=TEST_ATTEMPT_ID,
            ),
            user_id=7,
            session=session,
        )

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "DIALECT_CONFLICT"
    assert caught.value.detail["judge_status"] == "UNSUPPORTED"
    assert caught.value.detail["dialect_resolution"]["status"] == "DIALECT_CONFLICT"
    assert _TrackingSubmissionRepository.created == 0
    assert session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("judge_status", "expected_http_status"),
    [
        ("UNSUPPORTED", 422),
        ("ENGINE_ERROR", 503),
        ("TIMEOUT", 503),
    ],
)
async def test_platform_failure_does_not_write_learning_state(
    monkeypatch,
    judge_status,
    expected_http_status,
):
    question = SimpleNamespace(
        id=23,
        correct_sql="SELECT id FROM users",
        sql_dialect="postgres",
        schema_preview='{"tables":[{"name":"users","columns":["id"]}]}',
        difficulty=3,
    )
    session = _NoWriteSession()
    captured = {}
    _TrackingSubmissionRepository.created = 0
    leaked_error = (
        "runner failed: postgresql://user:secret@internal-db/private; "
        "SQL=SELECT password FROM users"
    )

    monkeypatch.setattr(
        ai_router,
        "QuestionRepository",
        lambda current_session: _ReadOnlyQuestionRepository(current_session, question),
    )
    monkeypatch.setattr(ai_router, "SubmissionRepository", _TrackingSubmissionRepository)

    def reject_write_repository(_session):
        raise AssertionError("platform failures must not reach chat or learning repositories")

    monkeypatch.setattr(ai_router, "ChatRepository", reject_write_repository)
    monkeypatch.setattr(
        ai_router.settings,
        "PARSEVAL_POSTGRES_URL",
        "postgresql://judge:secret@postgres/parseval",
    )
    monkeypatch.setattr(ai_router.settings, "PARSEVAL_EXECUTION_BACKEND", "auto")

    def failed_generate_and_compare(**kwargs):
        captured.update(kwargs)
        return parseval.SandboxRun(
            executed=False,
            is_equivalent=None,
            error=leaked_error if expected_http_status == 503 else f"simulated_{judge_status.lower()}",
            standard_sqlite=None,
            student_sqlite=None,
            standard_rows=[],
            student_rows=[],
            standard_columns=[],
            student_columns=[],
            test_database={},
            data_evidence={"judge_status": judge_status},
            mutation_evidence={},
            judge_status=judge_status,
        )

    monkeypatch.setattr(parseval, "generate_and_compare", failed_generate_and_compare)

    with pytest.raises(HTTPException) as caught:
        await check_sql(
            payload=SQLCheckRequest(
                student_sql="SELECT id FROM users",
                question_id=question.id,
                attempt_id=TEST_ATTEMPT_ID,
            ),
            user_id=11,
            session=session,
        )

    assert caught.value.status_code == expected_http_status
    assert caught.value.detail["code"] == judge_status
    assert caught.value.detail["judge_status"] == judge_status
    if expected_http_status == 503:
        assert caught.value.detail["message"] in {
            "SQL judge service is temporarily unavailable. Please try again later.",
            "SQL judge execution timed out. Please try again later.",
        }
        assert leaked_error not in str(caught.value.detail)
        assert "postgresql://" not in str(caught.value.detail)
        assert "internal-db" not in str(caught.value.detail)
        assert "secret" not in str(caught.value.detail)
        assert "SELECT password" not in str(caught.value.detail)
    assert caught.value.detail["dialect_resolution"]["resolved_dialect"] == "postgres"
    assert captured["native_executor_url"] == "postgresql://judge:secret@postgres/parseval"
    assert captured["execution_backend"] == "auto"
    assert _TrackingSubmissionRepository.created == 0
    assert session.commits == 0


def test_platform_error_public_message_redacts_runner_details(caplog):
    leaked_error = "connect postgresql://user:secret@internal-db/private while running SELECT password FROM users"

    with caplog.at_level("ERROR", logger="routers.ai"):
        with pytest.raises(HTTPException) as caught:
            ai_router._raise_platform_judge_error(
                judge_status="ENGINE_ERROR",
                error_message=leaked_error,
                error_code="DB_DRIVER_FAILURE",
            )

    assert caught.value.status_code == 503
    assert caught.value.detail == {
        "code": "ENGINE_ERROR",
        "judge_status": "ENGINE_ERROR",
        "message": "SQL judge service is temporarily unavailable. Please try again later.",
        "dialect_resolution": None,
    }
    assert leaked_error in caplog.text


def test_security_error_public_message_does_not_echo_details():
    leaked_error = "postgresql://user:secret@internal-db/private: SELECT password FROM users"

    with pytest.raises(HTTPException) as caught:
        ai_router._raise_platform_judge_error(
            judge_status="SECURITY_REJECTED",
            error_message=leaked_error,
            error_code="NATIVE_SQL_UNSAFE_FUNCTION",
        )

    assert caught.value.status_code == 422
    assert caught.value.detail["message"] == "SQL rejected by the sandbox safety policy."
    assert leaked_error not in str(caught.value.detail)


@pytest.mark.asyncio
async def test_native_security_rejection_returns_422_without_learning_writes(monkeypatch):
    question = SimpleNamespace(
        id=24,
        correct_sql="SELECT id FROM users",
        sql_dialect="postgres",
        schema_preview='{"tables":[{"name":"users","columns":["id"]}]}',
        difficulty=3,
    )
    session = _NoWriteSession()
    _TrackingSubmissionRepository.created = 0

    monkeypatch.setattr(
        ai_router,
        "QuestionRepository",
        lambda current_session: _ReadOnlyQuestionRepository(current_session, question),
    )
    monkeypatch.setattr(ai_router, "SubmissionRepository", _TrackingSubmissionRepository)

    def reject_write_repository(_session):
        raise AssertionError("security rejection must not write learning state")

    monkeypatch.setattr(ai_router, "ChatRepository", reject_write_repository)
    monkeypatch.setattr(ai_router.settings, "PARSEVAL_EXECUTION_BACKEND", "auto")
    monkeypatch.setattr(
        ai_router.settings,
        "PARSEVAL_POSTGRES_URL",
        "postgresql://judge:secret@postgres/parseval",
    )

    def rejected_generate_and_compare(**_kwargs):
        return parseval.SandboxRun(
            executed=False,
            is_equivalent=None,
            error="student_native_security_failed",
            standard_sqlite=None,
            student_sqlite=None,
            standard_rows=[],
            student_rows=[],
            standard_columns=[],
            student_columns=[],
            test_database={},
            data_evidence={
                "judge_status": "SECURITY_REJECTED",
                "error_code": "NATIVE_SQL_UNSAFE_FUNCTION",
            },
            mutation_evidence={},
            judge_status="SECURITY_REJECTED",
            error_code="NATIVE_SQL_UNSAFE_FUNCTION",
        )

    monkeypatch.setattr(parseval, "generate_and_compare", rejected_generate_and_compare)

    with pytest.raises(HTTPException) as caught:
        await check_sql(
            payload=SQLCheckRequest(
                student_sql="SELECT id FROM users",
                question_id=question.id,
                attempt_id=TEST_ATTEMPT_ID,
            ),
            user_id=11,
            session=session,
        )

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "NATIVE_SQL_UNSAFE_FUNCTION"
    assert caught.value.detail["judge_status"] == "SECURITY_REJECTED"
    assert _TrackingSubmissionRepository.created == 0
    assert session.commits == 0


@pytest.mark.asyncio
async def test_phase1_execution_is_offloaded_from_the_event_loop(monkeypatch):
    question = SimpleNamespace(
        id=29,
        correct_sql="SELECT id FROM users",
        sql_dialect="postgres",
        schema_preview='{"tables":[{"name":"users","columns":["id"]}]}',
        difficulty=3,
    )
    session = _NoWriteSession()
    captured = {}

    monkeypatch.setattr(
        ai_router,
        "QuestionRepository",
        lambda current_session: _ReadOnlyQuestionRepository(current_session, question),
    )
    monkeypatch.setattr(ai_router, "SubmissionRepository", _TrackingSubmissionRepository)
    monkeypatch.setattr(ai_router.settings, "PARSEVAL_EXECUTION_BACKEND", "auto")

    async def fake_to_thread(function, **kwargs):
        captured["function"] = function
        captured["kwargs"] = kwargs
        return parseval.SandboxRun(
            executed=False,
            is_equivalent=None,
            error="simulated_engine_error",
            standard_sqlite=None,
            student_sqlite=None,
            standard_rows=[],
            student_rows=[],
            standard_columns=[],
            student_columns=[],
            test_database={},
            data_evidence={"judge_status": "ENGINE_ERROR"},
            mutation_evidence={},
            judge_status="ENGINE_ERROR",
        )

    monkeypatch.setattr(ai_router.asyncio, "to_thread", fake_to_thread)

    with pytest.raises(HTTPException) as caught:
        await check_sql(
            payload=SQLCheckRequest(
                student_sql="SELECT id FROM users",
                question_id=question.id,
                attempt_id=TEST_ATTEMPT_ID,
            ),
            user_id=11,
            session=session,
        )

    assert caught.value.status_code == 503
    assert captured["function"] is parseval.generate_and_compare
    assert captured["kwargs"]["execution_backend"] == "auto"
