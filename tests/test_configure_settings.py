"""Tests for the configuration settings page helpers."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("sqlmodel")

from mytube import web


def test_settings_content_includes_device_loader_and_pairing_ui() -> None:
    content = web._settings_content(  # type: ignore[attr-defined]
        "/devices",
        {"youtube_app_auth": "stored"},
        "/save",
        "/pair",
    )

    assert "Loading devices..." in content
    assert "preferred-device" in content
    assert 'fetch("/devices")' in content
    assert "youtube-link-code" in content
    assert 'const pairEndpoint="/pair"' in content
    assert "settings-error-dialog" in content
    assert "Pairing..." in content


def test_settings_navigation_entry_available() -> None:
    assert ("settings", "Settings") in web.CONFIG_NAVIGATION  # type: ignore[attr-defined]


def test_pair_and_store_persists_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: dict[str, str] = {}

    def fake_pair(code: str) -> dict[str, str]:
        assert code == "ABCD"
        return {"token": "abcd"}

    def fake_store(values: dict[str, str]) -> None:
        stored.update(values)

    monkeypatch.setattr(web, "pair_with_link_code", fake_pair)  # type: ignore[attr-defined]
    monkeypatch.setattr(web, "store_settings", fake_store)  # type: ignore[attr-defined]

    result = web._pair_and_store("ABCD")  # type: ignore[attr-defined]

    assert result == {"token": "abcd"}
    assert stored["youtube_app_auth"] == '{"token":"abcd"}'
