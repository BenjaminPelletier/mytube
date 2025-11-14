"""Utilities for launching Chromecast sessions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

import pychromecast
from pychromecast.controllers.youtube import YouTubeController
from pychromecast.error import RequestFailed


_PLAYBACK_METHODS = {
    "play": "play",
    "pause": "pause",
    "stop": "stop",
}


class ChromecastUnavailableError(RuntimeError):
    """Raised when no Chromecast devices are available for casting."""


@dataclass
class CastResult:
    """Details about a Chromecast session that was requested."""

    device_name: str
    video_id: str


def _select_device(devices: Iterable[pychromecast.Chromecast], *, name: Optional[str] = None) -> pychromecast.Chromecast:
    discovered = list(devices)
    for device in discovered:
        if name is None or device.name == name:
            return device
    if name is None:
        message = "No Chromecast devices were discovered on the local network."
    else:
        message = f"No Chromecast device named '{name}' was found."
    available = ", ".join(device.name for device in discovered) or "<none>"
    logging.getLogger(__name__).warning("Chromecast discovery candidates: %s", available)
    raise ChromecastUnavailableError(message)


def discover_chromecast_names() -> list[str]:
    """Return the names of Chromecast devices that are currently discoverable."""

    chromecasts, browser = pychromecast.get_chromecasts()
    try:
        return [device.name for device in chromecasts]
    finally:
        if browser is not None:
            browser.stop_discovery()


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


def control_youtube_playback(action: str, *, device_name: str | None = None) -> str:
    """Send a playback control command to a Chromecast running YouTube."""

    normalized = action.lower()
    method_name = _PLAYBACK_METHODS.get(normalized)
    if method_name is None:
        raise ValueError(f"Unsupported playback action: {action!r}")

    chromecasts, browser = pychromecast.get_chromecasts()
    try:
        device = _select_device(chromecasts, name=device_name)
        device.wait()

        controller = getattr(device, "media_controller", None)
        if controller is None:
            raise ChromecastUnavailableError("Chromecast media controller is unavailable.")

        if hasattr(controller, "update_status"):
            controller.update_status()

        if hasattr(controller, "block_until_active"):
            is_active = controller.block_until_active(timeout=5)
            if not is_active:
                raise ChromecastUnavailableError(
                    "Chromecast has no active media session to control."
                )

        control_method = getattr(controller, method_name, None)
        if control_method is None:
            raise ChromecastUnavailableError(
                f"Chromecast does not support '{method_name}' playback control."
            )
        try:
            control_method()
        except RequestFailed as exc:
            raise ChromecastUnavailableError(
                f"Chromecast rejected '{method_name}' playback control: {exc}"
            ) from exc

        return device.name
    finally:
        if browser is not None:
            browser.stop_discovery()
