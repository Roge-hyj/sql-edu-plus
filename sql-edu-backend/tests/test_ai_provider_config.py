"""Tests for explicit, read-only CC Switch provider loading."""

from __future__ import annotations

import json
import sqlite3

from settings.ai_provider import load_cc_switch_provider


def test_load_cc_switch_provider_reads_responses_config_without_fallback(tmp_path) -> None:
    database = tmp_path / "cc-switch.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE providers (id TEXT NOT NULL, app_type TEXT NOT NULL, settings_config TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO providers (id, app_type, settings_config) VALUES (?, ?, ?)",
            (
                "jiji-provider",
                "codex",
                json.dumps(
                    {
                        "auth": {"OPENAI_API_KEY": "provider-secret"},
                        "config": (
                            'model_provider = "custom"\n'
                            'model = "gpt-5.6-sol"\n\n'
                            '[model_providers.custom]\n'
                            'wire_api = "responses"\n'
                            'base_url = "https://provider.example/v1"\n'
                        ),
                    }
                ),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    resolved = load_cc_switch_provider(
        database_path=str(database),
        provider_id="jiji-provider",
        app_type="codex",
    )

    assert resolved.base_url == "https://provider.example/v1"
    assert resolved.model == "gpt-5.6-sol"
    assert resolved.wire_api == "responses"
    assert resolved.api_key == "provider-secret"


def test_load_cc_switch_provider_requires_an_explicit_existing_provider(tmp_path) -> None:
    database = tmp_path / "cc-switch.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE providers (id TEXT NOT NULL, app_type TEXT NOT NULL, settings_config TEXT NOT NULL)"
        )
        connection.commit()
    finally:
        connection.close()

    try:
        load_cc_switch_provider(
            database_path=str(database),
            provider_id="missing",
            app_type="codex",
        )
    except ValueError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("missing provider must fail closed")
