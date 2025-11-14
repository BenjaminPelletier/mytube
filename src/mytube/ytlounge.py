"""Utilities for pairing with the YouTube TV lounge API."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pyytlounge

__all__ = ["PairingError", "pair_with_link_code"]


class PairingError(RuntimeError):
    """Raised when pairing with the YouTube app fails."""


_PAIRING_CALLABLE_NAMES = (
    "pair_link_code",
    "pair_code",
    "pair",
    "pair_with_code",
)


def pair_with_link_code(link_code: str) -> dict[str, Any]:
    """Pair with the YouTube TV app using a "Link with TV" code.

    Parameters
    ----------
    link_code:
        The code displayed by the YouTube application on a TV device. Hyphens,
        spaces, and other separator characters are ignored.

    Returns
    -------
    dict
        A mapping of authentication data returned by :mod:`pyytlounge`.

    Raises
    ------
    PairingError
        If the supplied code is invalid or the pairing library rejects it.
    """

    normalized_code = _normalize_code(link_code)
    if not normalized_code:
        raise PairingError("Enter the full Link with TV code from your TV.")

    callables = list(_iter_pairing_callables())
    if not callables:
        raise PairingError("pyytlounge pairing API is unavailable.")

    last_type_error: Exception | None = None
    for candidate in callables:
        try:
            result = candidate(normalized_code)
            if inspect.isawaitable(result):  # pragma: no branch - defensive
                result = asyncio.run(result)
        except TypeError as exc:  # pragma: no cover - depends on library shape
            last_type_error = exc
            continue
        except Exception as exc:  # pragma: no cover - defensive
            raise PairingError("Pairing with the YouTube app failed.") from exc

        return _normalize_auth_payload(result)

    raise PairingError("pyytlounge pairing API is unavailable.") from last_type_error


def _normalize_code(link_code: str) -> str:
    """Return a condensed version of the provided link code."""

    condensed = "".join(ch for ch in str(link_code) if ch.isalnum())
    if len(condensed) < 4:
        return ""
    return condensed.upper()


def _iter_pairing_callables() -> Sequence[Callable[[str], Any]]:
    """Yield pairing callables exposed by :mod:`pyytlounge`."""

    candidates: list[Callable[[str], Any]] = []

    modules_to_probe: list[Any] = [pyytlounge]
    pairing_module = getattr(pyytlounge, "pairing", None)
    if pairing_module is not None:
        modules_to_probe.append(pairing_module)

        client_cls = getattr(pairing_module, "PairingClient", None)
        if client_cls is not None:
            try:
                client = client_cls()
            except Exception:  # pragma: no cover - defensive
                client = None
            if client is not None:
                modules_to_probe.append(client)

    seen: set[int] = set()
    for module in modules_to_probe:
        for name in _PAIRING_CALLABLE_NAMES:
            attr = getattr(module, name, None)
            if callable(attr):
                identity = id(attr)
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(attr)

    return candidates


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
