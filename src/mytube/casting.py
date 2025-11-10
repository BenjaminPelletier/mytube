"""Utilities for launching Chromecast sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import pychromecast
from pychromecast.controllers.youtube import YouTubeController


class ChromecastUnavailableError(RuntimeError):
    """Raised when no Chromecast devices are available for casting."""


@dataclass
class CastResult:
    """Details about a Chromecast session that was requested."""

    device_name: str
    video_id: str


def _select_device(devices: Iterable[pychromecast.Chromecast], *, name: Optional[str] = None) -> pychromecast.Chromecast:
    for device in devices:
        if name is None or device.name == name:
            return device
    if name is None:
        message = "No Chromecast devices were discovered on the local network."
    else:
        message = f"No Chromecast device named '{name}' was found."
    raise ChromecastUnavailableError(message)


def cast_youtube_video(video_id: str, *, device_name: str | None = None) -> CastResult:
    """Cast a YouTube video to the first available Chromecast."""

    chromecasts, browser = pychromecast.get_chromecasts()
    try:
        device = _select_device(chromecasts, name=device_name)
        device.wait()

        controller = YouTubeController()
        device.register_handler(controller)
        controller.play_video(video_id)

        return CastResult(device_name=device.name, video_id=video_id)
    finally:
        if browser is not None:
            browser.stop_discovery()
