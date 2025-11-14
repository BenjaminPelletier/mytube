"""Tests for the configuration settings page helpers."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("sqlmodel")

from mytube import web


def test_settings_content_includes_device_loader_and_pairing_ui() -> None:
    content = web._settings_content(  # type: ignore[attr-defined]
        {"youtube_app_auth": "stored"},
        "/save",
        "/pair",
        {"connected": True},
    )

    assert content["save_url"] == "/save"
    assert content["pair_url"] == "/pair"
    assert content["settings"]["youtube_app_auth"] == "stored"
    assert content["lounge_status"] == {"connected": True}


def test_settings_navigation_entry_available() -> None:
    assert ("settings", "Settings") in web.CONFIG_NAVIGATION  # type: ignore[attr-defined]


def test_load_lounge_auth_handles_serialized_payload() -> None:
    payload = {"screenId": "screen-1", "lounge_id_token": "abc", "refresh_token": "xyz"}
    serialized = json.dumps(payload)

    result = web._load_lounge_auth({"youtube_app_auth": serialized})  # type: ignore[attr-defined]

    assert result is not None
    assert result["screenId"] == "screen-1"
    assert result["loungeIdToken"] == "abc"
