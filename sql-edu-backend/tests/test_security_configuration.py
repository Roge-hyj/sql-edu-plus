"""Regression tests for production-safe database logging and CORS defaults."""

from __future__ import annotations

from pathlib import Path
import pytest
from fastapi.middleware.cors import CORSMiddleware

from main import app
from models import engine
from routers.auth import resolve_registration_role
from settings.config import Settings, settings, validate_business_db_url, validate_phase1_worker_config


def _cors_options() -> dict[str, object]:
    middleware = next(
        item for item in app.user_middleware if item.cls is CORSMiddleware
    )
    return middleware.kwargs


def test_database_echo_is_opt_in_and_current_engine_uses_configured_value() -> None:
    assert Settings.model_fields["DB_ECHO"].default is False
    assert engine.echo is settings.DB_ECHO


def test_business_database_contract_is_single_mysql_runtime() -> None:
    assert Settings.model_fields["BUSINESS_DB_DIALECT"].default == "mysql"
    assert Settings.model_fields["BUSINESS_DB_VERSION"].default == "8.0.46"
    assert Settings.model_fields["BUSINESS_DB_CHARSET"].default == "utf8mb4"


def test_native_mysql_example_matches_fixed_contract_version() -> None:
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "PARSEVAL_MYSQL_VERSION=8.0.46" in example


def test_phase1_worker_thread_mode_is_rejected_outside_debug() -> None:
    with pytest.raises(RuntimeError, match="禁止.*thread"):
        validate_phase1_worker_config(
            mode="thread",
            debug=False,
            start_method="spawn",
            max_concurrency=2,
            queue_limit=8,
            memory_mb=2048,
            cpu_seconds=50,
        )


def test_phase1_worker_process_mode_accepts_bounded_production_defaults() -> None:
    validate_phase1_worker_config(
        mode="process",
        debug=False,
        start_method="spawn",
        max_concurrency=2,
        queue_limit=8,
        memory_mb=2048,
        cpu_seconds=50,
    )


def test_phase1_worker_limits_are_explicit_and_bounded() -> None:
    fields = Settings.model_fields
    assert fields["PARSEVAL_WORKER_MAX_CONCURRENCY"].default == 2
    assert fields["PARSEVAL_WORKER_QUEUE_LIMIT"].default == 8
    assert fields["PARSEVAL_WORKER_MEMORY_MB"].default == 2048
    assert fields["PARSEVAL_WORKER_CPU_SECONDS"].default == 50

    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    for key in (
        "PARSEVAL_WORKER_MAX_CONCURRENCY=2",
        "PARSEVAL_WORKER_QUEUE_LIMIT=8",
        "PARSEVAL_WORKER_MEMORY_MB=2048",
        "PARSEVAL_WORKER_CPU_SECONDS=50",
    ):
        assert key in example


@pytest.mark.parametrize(
    ("db_url", "debug", "valid"),
    [
        ("mysql+aiomysql://user:pass@db/sql_edu", False, True),
        ("sqlite+aiosqlite:///:memory:", True, True),
        ("sqlite+aiosqlite:///:memory:", False, False),
        ("postgresql+asyncpg://user:pass@db/sql_edu", False, False),
    ],
)
def test_business_database_url_validation(db_url: str, debug: bool, valid: bool) -> None:
    if valid:
        validate_business_db_url(db_url, debug=debug)
    else:
        with pytest.raises(RuntimeError, match="业务数据库配置不受支持"):
            validate_business_db_url(db_url, debug=debug)


def test_mail_test_endpoint_is_disabled_by_default() -> None:
    assert Settings.model_fields["ENABLE_MAIL_TEST"].default is False
    assert not any(route.path == "/mail/test" for route in app.routes)


def test_teacher_invite_is_disabled_without_a_configured_secret() -> None:
    assert Settings.model_fields["TEACHER_INVITE_CODE"].default == ""


@pytest.mark.parametrize(
    ("configured", "supplied", "expected_role", "expected_error"),
    [
        ("", None, "student", None),
        ("teacher-secret", None, "student", None),
        ("", "anything", None, "教师邀请码无效或当前未启用"),
        ("teacher-secret", "wrong", None, "教师邀请码无效或当前未启用"),
        ("teacher-secret", " teacher-secret ", "teacher", None),
    ],
)
def test_teacher_invite_registration_policy(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    supplied: str | None,
    expected_role: str | None,
    expected_error: str | None,
) -> None:
    monkeypatch.setattr(settings, "TEACHER_INVITE_CODE", configured)

    role, error = resolve_registration_role(supplied)

    assert role == expected_role
    assert error == expected_error


def test_cors_uses_explicit_origins_and_restricted_request_surface() -> None:
    options = _cors_options()
    origins = options["allow_origins"]

    assert origins == settings.BACKEND_CORS_ORIGINS
    assert "*" not in origins
    assert options["allow_methods"] == [
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ]
    assert options["allow_headers"] == ["Authorization", "Content-Type", "Accept"]
    assert options["allow_credentials"] is True
