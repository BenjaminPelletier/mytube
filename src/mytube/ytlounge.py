"""Utilities for pairing with the YouTube TV lounge API."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pyytlounge.wrapper import YtLoungeApi

__all__ = ["PairingError", "pair_with_link_code", "dumps_auth_payload"]


class PairingError(RuntimeError):
    """Raised when pairing with the YouTube app fails."""


def pair_with_link_code(link_code: str) -> dict[str, Any]:
    """Pair with the YouTube TV app using a "Link with TV" code."""

    normalized_code = _normalize_code(link_code)
    if not normalized_code:
        raise PairingError("Enter the full Link with TV code from your TV.")

    async def _pair() -> Any:
        async with YtLoungeApi("MyTube Remote") as api:
            paired = await api.pair(normalized_code)
            if not paired:
                raise PairingError("Unable to pair with the YouTube app.")
            payload = api.store_auth_state()
            return _normalize_auth_payload(payload)

    return asyncio.run(_pair())


def _normalize_code(link_code: str) -> str:
    """Return a condensed version of the provided link code."""

    condensed = "".join(ch for ch in str(link_code) if ch.isalnum())
    if len(condensed) < 4:
        return ""
    return condensed.upper()


def _normalize_auth_payload(result: Any) -> dict[str, Any]:
    """Coerce the pairing result into a JSON-serializable mapping."""

    if isinstance(result, Mapping):
        return {str(key): value for key, value in result.items()}

    if dataclasses.is_dataclass(result):
        return dataclasses.asdict(result)

    if hasattr(result, "model_dump"):
        try:
            dumped = result.model_dump()
        except Exception:  # pragma: no cover - defensive
            dumped = None
        if isinstance(dumped, Mapping):
            return {str(key): value for key, value in dumped.items()}

    if hasattr(result, "dict"):
        try:
            dumped = result.dict()
        except Exception:  # pragma: no cover - defensive
            dumped = None
        if isinstance(dumped, Mapping):
            return {str(key): value for key, value in dumped.items()}

    if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
        if result and isinstance(result[0], Mapping):
            return {str(key): value for key, value in result[0].items()}
        return {"values": list(result)}

    return {"value": result}


def dumps_auth_payload(payload: Mapping[str, Any]) -> str:
    """Serialize the pairing payload to a compact JSON string."""

    return json.dumps(payload, default=_json_default, separators=(",", ":"))


def _json_default(obj: Any) -> Any:
    """Fallback serializer used for complex objects."""

    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "model_dump"):
        try:
            dumped = obj.model_dump()
        except Exception:  # pragma: no cover - defensive
            dumped = None
        if dumped is not None:
            return dumped
    if hasattr(obj, "dict"):
        try:
            dumped = obj.dict()
        except Exception:  # pragma: no cover - defensive
            dumped = None
        if dumped is not None:
            return dumped
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)
