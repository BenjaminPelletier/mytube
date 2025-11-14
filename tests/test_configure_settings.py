"""Tests for the configuration settings page helpers."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("sqlmodel")

from mytube import web


def test_settings_content_includes_device_loader() -> None:
    content = web._settings_content("/devices")  # type: ignore[attr-defined]

    assert "Loading devices..." in content
    assert "preferred-device" in content
    assert 'fetch("/devices")' in content
    assert "setSingleOption('No devices found')" in content
    assert "Device choice will be saved in a future update." in content


def test_settings_navigation_entry_available() -> None:
    assert ("settings", "Settings") in web.CONFIG_NAVIGATION  # type: ignore[attr-defined]
