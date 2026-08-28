"""Explicit, read-only AI provider resolution helpers.

The development workstation uses CC Switch to hold provider credentials.  The
backend may opt into one provider by pointing at the CC Switch SQLite database
and provider id; credentials are read at runtime and are never copied into
source files, logs, or response payloads.  Deployments that do not configure
these fields continue to use the ordinary ``AI_*`` environment settings.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import tomllib
from typing import Any, Literal


AIWireAPI = Literal["chat_completions", "responses"]


@dataclass(frozen=True, slots=True)
class CCProviderConfig:
    """The minimum provider material needed by an OpenAI-compatible client."""

    api_key: str
    base_url: str
    model: str
    wire_api: AIWireAPI


def _config_value(config_text: str, key: str) -> str:
    """Read a simple TOML provider field without exposing its value."""

    try:
        parsed = tomllib.loads(config_text)
    except (TypeError, tomllib.TOMLDecodeError):
        parsed = {}
    if key == "model":
        value = parsed.get("model")
    else:
        provider_blocks = parsed.get("model_providers")
        value = None
        if isinstance(provider_blocks, dict):
            for block in provider_blocks.values():
                if isinstance(block, dict) and isinstance(block.get(key), str):
                    value = block[key]
                    break
    if isinstance(value, str) and value.strip():
        return value.strip()

    # CC Switch config is TOML today.  This bounded fallback keeps the loader
    # useful for an older config export while accepting only quoted scalars.
    match = re.search(
        rf"(?m)^\s*{re.escape(key)}\s*=\s*[\"']([^\"']+)[\"']\s*$",
        config_text,
    )
    return match.group(1).strip() if match else ""


def _wire_api(value: Any) -> AIWireAPI:
    return "responses" if str(value or "").strip().lower() == "responses" else "chat_completions"


def load_cc_switch_provider(
    *,
    database_path: str,
    provider_id: str,
    app_type: str = "codex",
) -> CCProviderConfig:
    """Load one explicitly selected CC Switch provider in read-only mode.

    ``database_path`` and ``provider_id`` are both required by design.  An
    invalid explicit configuration raises a bounded configuration error at
    startup instead of silently using a different account or endpoint.
    """

    path_text = str(database_path or "").strip()
    provider_text = str(provider_id or "").strip()
    app_text = str(app_type or "").strip() or "codex"
    if not path_text or not provider_text:
        raise ValueError("CC Switch provider requires database_path and provider_id")

    path = Path(path_text).expanduser()
    if not path.is_file():
        raise ValueError("CC Switch provider database does not exist")

    uri = f"file:{path.resolve()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = connection.execute(
                "SELECT settings_config FROM providers WHERE id = ? AND app_type = ?",
                (provider_text, app_text),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError("CC Switch provider database cannot be read") from exc
    if not row or not isinstance(row[0], str):
        raise ValueError("CC Switch provider was not found")

    try:
        settings_config = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("CC Switch provider configuration is invalid") from exc
    if not isinstance(settings_config, dict):
        raise ValueError("CC Switch provider configuration is invalid")

    auth = settings_config.get("auth")
    api_key = ""
    if isinstance(auth, dict):
        for key in ("OPENAI_API_KEY", "API_KEY", "api_key"):
            candidate = auth.get(key)
            if isinstance(candidate, str) and candidate.strip():
                api_key = candidate.strip()
                break
    config_text = settings_config.get("config")
    if not isinstance(config_text, str):
        config_text = ""
    model = _config_value(config_text, "model")
    base_url = _config_value(config_text, "base_url").rstrip("/")
    try:
        parsed = tomllib.loads(config_text)
    except (TypeError, tomllib.TOMLDecodeError):
        parsed = {}
    wire_value: Any = ""
    provider_blocks = parsed.get("model_providers") if isinstance(parsed, dict) else None
    if isinstance(provider_blocks, dict):
        for block in provider_blocks.values():
            if isinstance(block, dict) and block.get("wire_api"):
                wire_value = block.get("wire_api")
                break

    if not api_key or not base_url or not model:
        raise ValueError("CC Switch provider is missing key, base URL, or model")
    if not base_url.startswith(("https://", "http://")):
        raise ValueError("CC Switch provider base URL is unsupported")
    return CCProviderConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        wire_api=_wire_api(wire_value),
    )


__all__ = ["AIWireAPI", "CCProviderConfig", "load_cc_switch_provider"]
