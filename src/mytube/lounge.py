"""Persistent client management for YouTube Lounge connections."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

try:  # pragma: no cover - optional dependency resolution
    from pyytlounge.exceptions import NotConnectedException, NotLinkedException, NotPairedException
except ModuleNotFoundError:  # pragma: no cover - fallback definitions for tests
    class NotConnectedException(RuntimeError):
        """Fallback error when pyytlounge is unavailable."""

    class NotLinkedException(RuntimeError):
        """Fallback error when pyytlounge is unavailable."""

    class NotPairedException(RuntimeError):
        """Fallback error when pyytlounge is unavailable."""

from .ytlounge import PairingError

__all__ = ["LoungeController", "LoungeManager", "coerce_auth_state", "normalize_link_code"]


logger = logging.getLogger(__name__)

_CONNECTION_RETRIES = 6
_BASE_BACKOFF = 0.5
_MAX_BACKOFF = 10.0

_YT_LOUNGE_API_CLASS: type | None = None


def _get_yt_lounge_api_class() -> type:
    """Return the :class:`pyytlounge.wrapper.YtLoungeApi` class."""

    global _YT_LOUNGE_API_CLASS
    if _YT_LOUNGE_API_CLASS is None:
        try:
            from pyytlounge.wrapper import YtLoungeApi as api_class  # type: ignore import
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "pyytlounge is not available to manage lounge connections."
            ) from exc
        _YT_LOUNGE_API_CLASS = api_class
    return _YT_LOUNGE_API_CLASS


def normalize_link_code(link_code: str) -> str:
    """Normalize a link-with-TV code by stripping separators."""

    condensed = "".join(ch for ch in str(link_code) if ch.isalnum())
    if len(condensed) < 4:
        return ""
    return condensed.upper()


def coerce_auth_state(auth_state: Mapping[str, Any] | str) -> dict[str, Any]:
    """Return an auth payload compatible with :class:`YtLoungeApi`."""

    if isinstance(auth_state, str):
        try:
            parsed = json.loads(auth_state)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise ValueError("Invalid serialized auth payload") from exc
        if not isinstance(parsed, Mapping):
            raise ValueError("Auth payload must deserialize to a mapping")
        auth_state = parsed

    if not isinstance(auth_state, Mapping):  # pragma: no cover - defensive
        raise ValueError("Auth state must be a mapping")

    version = int(auth_state.get("version") or 1)
    screen_id = (
        auth_state.get("screenId")
        or auth_state.get("screen_id")
        or auth_state.get("screenID")
    )
    lounge_token = (
        auth_state.get("loungeIdToken")
        or auth_state.get("lounge_id_token")
        or auth_state.get("loungeToken")
        or auth_state.get("lounge_token")
    )
    refresh_token = auth_state.get("refreshToken") or auth_state.get("refresh_token")
    expiry = auth_state.get("expiry")

    if not screen_id or not lounge_token:
        raise ValueError("Auth payload missing lounge token or screen identifier")

    return {
        "version": version,
        "screenId": str(screen_id),
        "loungeIdToken": lounge_token,
        "refreshToken": refresh_token,
        "expiry": expiry,
    }


class LoungeController:
    """Wrap a single TV connection with locking & reconnects."""

    def __init__(
        self,
        name: str,
        auth_state: Mapping[str, Any] | str | None = None,
    ) -> None:
        self.name = name
        self._auth_state: dict[str, Any] | None = None
        self.screen_id: str | None = None
        if auth_state is not None:
            self._apply_auth_state(auth_state, loaded=False)
        self._api: Any | None = None
        self._api_has_auth = False
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._ready = asyncio.Event()

    def _apply_auth_state(
        self,
        auth_state: Mapping[str, Any] | str,
        *,
        loaded: bool,
    ) -> dict[str, Any]:
        normalized = coerce_auth_state(auth_state)
        self._auth_state = normalized
        self.screen_id = normalized["screenId"]
        self._api_has_auth = loaded
        self._ready.clear()
        return normalized

    def _require_auth_state(self) -> dict[str, Any]:
        if self._auth_state is None:
            raise RuntimeError("TV is not paired with the YouTube app.")
        return {
            "version": self._auth_state["version"],
            "screenId": self._auth_state["screenId"],
            "loungeIdToken": self._auth_state["loungeIdToken"],
            "refreshToken": self._auth_state.get("refreshToken"),
            "expiry": self._auth_state.get("expiry"),
        }

    async def _ensure_api(self) -> Any:
        api = self._api
        if api is None:
            api_class = _get_yt_lounge_api_class()
            api = api_class(self.name)
            await api.__aenter__()
            self._api = api
            self._api_has_auth = False
        return api

    async def connect(self) -> None:
        """Idempotently establish a lounge connection with backoff."""

        async with self._connect_lock:
            api = await self._ensure_api()
            auth_payload = self._require_auth_state()
            if not self._api_has_auth:
                api.load_auth_state(auth_payload)
                self._api_has_auth = True
            if api.connected():
                self._ready.set()
                return

            last_error: Exception | None = None
            for attempt in range(_CONNECTION_RETRIES):
                try:
                    connected = await api.connect()
                except (NotLinkedException, NotPairedException, NotConnectedException) as exc:
                    last_error = exc
                    logger.warning(
                        "YouTube Lounge connection failed for %s: %s",
                        self.screen_id or "unknown screen",
                        exc,
                    )
                else:
                    if connected:
                        self._ready.set()
                        return
                    try:
                        await api.refresh_auth()
                    except (NotLinkedException, NotPairedException, NotConnectedException) as exc:
                        last_error = exc
                        logger.warning(
                            "Refreshing YouTube Lounge auth failed for %s: %s",
                            self.screen_id or "unknown screen",
                            exc,
                        )
                backoff = min(_BASE_BACKOFF * (2**attempt), _MAX_BACKOFF)
                await asyncio.sleep(backoff)

            self._ready.clear()
            raise RuntimeError("Unable to connect to TV YouTube app") from last_error

    async def ensure_connected(self) -> None:
        await self.connect()

    async def close(self) -> None:
        api, self._api = self._api, None
        self._api_has_auth = False
        self._ready.clear()
        if api is not None:
            try:
                await api.close()
            except asyncio.CancelledError:  # pragma: no cover - propagate cancellations
                raise
            except Exception:  # pragma: no cover - best effort cleanup
                logger.warning("Error while closing lounge connection", exc_info=True)

    async def update_auth(
        self,
        auth_state: Mapping[str, Any] | str,
        *,
        name: str | None = None,
    ) -> None:
        """Replace stored authentication data and reset the connection."""

        async with self._lock:
            if name:
                self.name = name
            self._apply_auth_state(auth_state, loaded=False)
            await self.close()

    async def pair_with_code(self, link_code: str) -> dict[str, Any]:
        """Pair this controller with the YouTube TV app."""

        normalized_code = normalize_link_code(link_code)
        if not normalized_code:
            raise PairingError("Enter the full Link with TV code from your TV.")

        async with self._lock:
            api = await self._ensure_api()
            paired = await api.pair(normalized_code)
            if not paired:
                raise PairingError("Unable to pair with the YouTube app.")
            payload = api.store_auth_state()
            normalized_payload = self._apply_auth_state(payload, loaded=True)
        await self.connect()
        return normalized_payload

    async def get_status(self) -> dict[str, Any]:
        """Return connection information about the controller."""

        status: dict[str, Any] = {
            "screen_id": self.screen_id,
            "name": self.name,
            "connected": False,
        }

        if self._auth_state is None:
            status["error"] = "TV is not paired with the YouTube app."
            return status

        try:
            await self.ensure_connected()
        except (RuntimeError, NotConnectedException, NotLinkedException, NotPairedException) as exc:
            status["error"] = str(exc)
            return status

        api = self._api
        if api is None:
            return status

        status["connected"] = api.connected()
        status["screen_name"] = getattr(api, "screen_name", None)
        status["device_name"] = getattr(api, "screen_device_name", None)
        return status


class LoungeManager:
    """Manages persistent connections for one or more TVs."""

    def __init__(self, *, default_name: str = "MyTube Remote"):
        self._controllers: dict[str, LoungeController] = {}
        self._lock = asyncio.Lock()
        self._default_name = default_name

    async def get(self, screen_id: str) -> LoungeController | None:
        async with self._lock:
            return self._controllers.get(screen_id)

    async def upsert_from_auth(
        self,
        auth_state: Mapping[str, Any] | str,
        *,
        name: str | None = None,
    ) -> LoungeController:
        normalized = coerce_auth_state(auth_state)
        screen_id = normalized["screenId"]
        async with self._lock:
            controller = self._controllers.get(screen_id)
            if controller is None:
                controller = LoungeController(name or self._default_name, normalized)
                self._controllers[screen_id] = controller
            else:
                await controller.update_auth(normalized, name=name)
        await controller.ensure_connected()
        return controller

    async def pair_with_code(
        self,
        link_code: str,
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        controller = LoungeController(name or self._default_name)
        payload = await controller.pair_with_code(link_code)
        screen_id = controller.screen_id
        if not screen_id:  # pragma: no cover - defensive
            raise PairingError("Unable to determine screen identifier from pairing response.")

        async with self._lock:
            existing = self._controllers.get(screen_id)
            if existing is None:
                self._controllers[screen_id] = controller
            else:
                await existing.update_auth(payload, name=name)
                await controller.close()
                controller = existing
        await controller.ensure_connected()
        return payload

    async def shutdown(self) -> None:
        async with self._lock:
            controllers = list(self._controllers.values())
            self._controllers.clear()

        await asyncio.gather(*(controller.close() for controller in controllers), return_exceptions=True)
