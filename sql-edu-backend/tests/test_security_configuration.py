"""Regression tests for production-safe database logging and CORS defaults."""

from __future__ import annotations

from fastapi.middleware.cors import CORSMiddleware

from main import app
from models import engine
from settings.config import Settings, settings


def _cors_options() -> dict[str, object]:
    middleware = next(
        item for item in app.user_middleware if item.cls is CORSMiddleware
    )
    return middleware.kwargs


def test_database_echo_is_opt_in_and_current_engine_uses_configured_value() -> None:
    assert Settings.model_fields["DB_ECHO"].default is False
    assert engine.echo is settings.DB_ECHO


def test_mail_test_endpoint_is_disabled_by_default() -> None:
    assert Settings.model_fields["ENABLE_MAIL_TEST"].default is False
    assert not any(route.path == "/mail/test" for route in app.routes)


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
