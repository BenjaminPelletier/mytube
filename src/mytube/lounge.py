"""Persistent client management for YouTube Lounge connections."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from pyytlounge.exceptions import NotConnectedException, NotLinkedException, NotPairedException
from pyytlounge.wrapper import YtLoungeApi

from .ytlounge import PairingError

__all__ = ["LoungeController", "LoungeManager", "coerce_auth_state", "normalize_link_code"]


logger = logging.getLogger(__name__)

_CONNECTION_RETRIES = 6
_BASE_BACKOFF = 0.5
_MAX_BACKOFF = 10.0


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

    def __init__(self, name: str, auth_state: Mapping[str, Any]):
        self.name = name
        self._auth_state = coerce_auth_state(auth_state)
        self.screen_id = self._auth_state["screenId"]
        self._api: YtLoungeApi | None = None
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def _initialize_api(self) -> YtLoungeApi:
        api = YtLoungeApi(self.name)
        await api.__aenter__()
        api.load_auth_state({
            "version": self._auth_state["version"],
            "screenId": self._auth_state["screenId"],
            "loungeIdToken": self._auth_state["loungeIdToken"],
            "refreshToken": self._auth_state.get("refreshToken"),
            "expiry": self._auth_state.get("expiry"),
        })
        return api

    async def connect(self) -> None:
        """Idempotently establish a lounge connection with backoff."""

        async with self._connect_lock:
            if self._api and self._api.connected():
                return

            last_error: Exception | None = None
            for attempt in range(_CONNECTION_RETRIES):
                api: YtLoungeApi | None = None
                try:
                    api = await self._initialize_api()
                    if not await api.connect():
                        await api.refresh_auth()
                        if not await api.connect():
                            raise RuntimeError("Unable to connect to TV YouTube app")

                    self._api = api
                    self._ready.set()
                    return
                except (NotLinkedException, NotPairedException, NotConnectedException) as exc:
                    last_error = exc
                    logger.warning("YouTube Lounge connection lost for %s: %s", self.screen_id, exc)
                except Exception as exc:  # pragma: no cover - defensive
                    last_error = exc
                    logger.exception("Unexpected error while connecting to YouTube lounge")
                finally:
                    if self._api is not api and api is not None:
                        try:
                            await api.close()
                        except Exception:  # pragma: no cover - best effort cleanup
                            pass

                backoff = min(_BASE_BACKOFF * (2**attempt), _MAX_BACKOFF)
                await asyncio.sleep(backoff)

            self._ready.clear()
            self._api = None
            if last_error is None:
                raise RuntimeError("Unable to connect to TV YouTube app")
            raise RuntimeError("Unable to connect to TV YouTube app") from last_error

    async def ensure_connected(self) -> None:
        if not self._api or not self._api.connected():
            await self.connect()

    async def close(self) -> None:
        api, self._api = self._api, None
        self._ready.clear()
        if api is not None:
            try:
                await api.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.exception("Error while closing lounge connection")

    async def update_auth(self, auth_state: Mapping[str, Any], *, name: str | None = None) -> None:
        """Replace stored authentication data and reset the connection."""

        new_state = coerce_auth_state(auth_state)
        async with self._lock:
            if name:
                self.name = name
            self._auth_state = new_state
            self.screen_id = new_state["screenId"]
            await self.close()

    async def get_status(self) -> dict[str, Any]:
        """Return connection information about the controller."""

        status: dict[str, Any] = {
            "screen_id": self.screen_id,
            "name": self.name,
            "connected": False,
        }

        try:
            await self.ensure_connected()
            api = self._api
            if api is None:
                return status
            status["connected"] = api.connected()
            try:
                status["screen_name"] = api.screen_name
            except Exception:  # pragma: no cover - attribute access best effort
                status["screen_name"] = None
            try:
                status["device_name"] = api.screen_device_name
            except Exception:  # pragma: no cover - attribute access best effort
                status["device_name"] = None
        except Exception as exc:
            status["error"] = str(exc)
            await self.close()
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
        auth_state: Mapping[str, Any],
        *,
        name: str | None = None,
    ) -> LoungeController:
        normalized = coerce_auth_state(auth_state)
        screen_id = normalized["screenId"]
        controller: LoungeController | None = None
        created = False
        async with self._lock:
            controller = self._controllers.get(screen_id)
            if controller is None:
                controller = LoungeController(name or self._default_name, normalized)
                self._controllers[screen_id] = controller
                created = True

        if controller is None:  # pragma: no cover - defensive
            raise RuntimeError("Unable to create lounge controller")

        if not created:
            await controller.update_auth(normalized, name=name)
        await controller.ensure_connected()
        return controller

    async def pair_with_code(
        self,
        link_code: str,
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        normalized_code = normalize_link_code(link_code)
        if not normalized_code:
            raise PairingError("Enter the full Link with TV code from your TV.")

        device_name = name or self._default_name
        async with YtLoungeApi(device_name) as api:
            paired = await api.pair(normalized_code)
            if not paired:
                raise PairingError("Unable to pair with the YouTube app.")
            payload = api.store_auth_state()

        await self.upsert_from_auth(payload, name=device_name)
        return payload

    async def shutdown(self) -> None:
        controllers: list[LoungeController]
        async with self._lock:
            controllers = list(self._controllers.values())
            self._controllers.clear()

        await asyncio.gather(*(controller.close() for controller in controllers), return_exceptions=True)

