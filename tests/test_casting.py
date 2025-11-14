"""Tests for Chromecast casting helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "mytube" / "casting.py"
SPEC = importlib.util.spec_from_file_location("mytube_casting_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - safety check
    raise RuntimeError("Unable to load casting module for tests")

casting = importlib.util.module_from_spec(SPEC)
sys.modules["mytube_casting_test"] = casting
SPEC.loader.exec_module(casting)

ChromecastUnavailableError = casting.ChromecastUnavailableError
control_youtube_playback = casting.control_youtube_playback


class _DummyBrowser:
    def __init__(self) -> None:
        self.stopped = False

    def stop_discovery(self) -> None:  # pragma: no cover - simple stub
        self.stopped = True


def test_control_playback_reports_request_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from pychromecast.error import RequestFailed

    class _FailingController:
        def update_status(self) -> None:
            return None

        def block_until_active(self, timeout: float | None = None) -> bool:
            return True

        def pause(self) -> None:
            raise RequestFailed("Failed to execute pause.")

    class _Device:
        name = "Living Room"

        def __init__(self) -> None:
            self.media_controller = _FailingController()

        def wait(self) -> None:
            return None

    device = _Device()
    browser = _DummyBrowser()

    def _fake_get_chromecasts():
        return [device], browser

    def _fake_select_device(devices, name=None):
        return devices[0]

    monkeypatch.setattr(casting.pychromecast, "get_chromecasts", _fake_get_chromecasts)
    monkeypatch.setattr(casting, "_select_device", _fake_select_device)

    with pytest.raises(ChromecastUnavailableError) as excinfo:
        control_youtube_playback("pause")

    message = str(excinfo.value)
    assert "Chromecast rejected 'pause' playback control" in message
    assert "Failed to execute pause." in message
    assert browser.stopped is True


def test_control_playback_requires_active_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class _InactiveController:
        def update_status(self) -> None:
            return None

        def block_until_active(self, timeout: float | None = None) -> bool:
            return False

    class _Device:
        name = "Living Room"

        def __init__(self) -> None:
            self.media_controller = _InactiveController()

        def wait(self) -> None:
            return None

    device = _Device()
    browser = _DummyBrowser()

    def _fake_get_chromecasts():
        return [device], browser

    def _fake_select_device(devices, name=None):
        return devices[0]

    monkeypatch.setattr(casting.pychromecast, "get_chromecasts", _fake_get_chromecasts)
    monkeypatch.setattr(casting, "_select_device", _fake_select_device)

    with pytest.raises(ChromecastUnavailableError) as excinfo:
        control_youtube_playback("pause")

    message = str(excinfo.value)
    assert "no active media session" in message
    assert browser.stopped is True
