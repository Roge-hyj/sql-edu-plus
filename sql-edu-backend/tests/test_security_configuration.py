"""Regression tests for production-safe database logging and CORS defaults."""

from __future__ import annotations

import pytest
from fastapi.middleware.cors import CORSMiddleware

from main import app
from models import engine
from routers.auth import resolve_registration_role
from settings.config import Settings, settings, validate_business_db_url


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
    assert Settings.model_fields["BUSINESS_DB_VERSION"].default == "8.4"
    assert Settings.model_fields["BUSINESS_DB_CHARSET"].default == "utf8mb4"


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
