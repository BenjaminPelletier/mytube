"""Utilities for pairing with the YouTube TV lounge API."""

from __future__ import annotations

import importlib
import inspect

import asyncio
import dataclasses
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["PairingError", "pair_with_link_code"]


logger = logging.getLogger(__name__)

pyytlounge: Any | None = None


class PairingError(RuntimeError):
    """Raised when pairing with the YouTube app fails."""


_PAIRING_CALLABLE_NAMES = (
    "pair_link_code",
    "pair_code",
    "pair",
    "pair_with_code",
)


def _resolve_pyytlounge() -> Any:
    global pyytlounge
    if pyytlounge is None:
        try:
            pyytlounge = importlib.import_module("pyytlounge")
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
            raise PairingError("pyytlounge is not available to complete pairing.") from exc
    return pyytlounge


def _find_api_class(module: Any) -> Any | None:
    api = getattr(module, "YtLoungeApi", None)
    if api is None:
        wrapper = getattr(module, "wrapper", None)
        if wrapper is not None:
            api = getattr(wrapper, "YtLoungeApi", None)
    return api


def _module_name(obj: Any) -> str:
    if hasattr(obj, "__name__"):
        return getattr(obj, "__name__")  # type: ignore[return-value]
    return obj.__class__.__name__


def _call_with_code(func: Any, code: str) -> Any:
    result = func(code)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _pair_via_legacy(module: Any, code: str) -> Any:
    attempts: list[str] = []

    def _try_callable(func: Any, description: str) -> tuple[bool, Any | None]:
        if not callable(func):
            return False, None
        try:
            return True, _call_with_code(func, code)
        except Exception as exc:  # pragma: no cover - diagnostic path
            attempts.append(f"{description} raised {exc.__class__.__name__}: {exc}")
            return False, None

    pairing_module = getattr(module, "pairing", None)
    module_name = _module_name(module)
    pairing_module_name = _module_name(pairing_module) if pairing_module is not None else ""

    if pairing_module is not None:
        client_factory = getattr(pairing_module, "PairingClient", None)
        if callable(client_factory):
            try:
                client = client_factory()
            except Exception as exc:  # pragma: no cover - diagnostic path
                attempts.append(
                    f"{pairing_module_name}.PairingClient raised {exc.__class__.__name__}: {exc}"
                )
            else:
                for name in _PAIRING_CALLABLE_NAMES:
                    success, result = _try_callable(
                        getattr(client, name, None),
                        f"{pairing_module_name}.PairingClient.{name}",
                    )
                    if success:
                        return result

        for name in _PAIRING_CALLABLE_NAMES:
            success, result = _try_callable(
                getattr(pairing_module, name, None),
                f"{pairing_module_name}.{name}",
            )
            if success:
                return result

    client_factory = getattr(module, "PairingClient", None)
    if callable(client_factory):
        try:
            client = client_factory()
        except Exception as exc:  # pragma: no cover - diagnostic path
            attempts.append(
                f"{module_name}.PairingClient raised {exc.__class__.__name__}: {exc}"
            )
        else:
            for name in _PAIRING_CALLABLE_NAMES:
                success, result = _try_callable(
                    getattr(client, name, None),
                    f"{module_name}.PairingClient.{name}",
                )
                if success:
                    return result

    for name in _PAIRING_CALLABLE_NAMES:
        success, result = _try_callable(getattr(module, name, None), f"{module_name}.{name}")
        if success:
            return result

    diagnostics: list[str] = []
    version = getattr(module, "__version__", None)
    if version:
        diagnostics.append(f"pyytlounge version {version}")

    if pairing_module is not None:
        available = [
            name
            for name in dir(pairing_module)
            if callable(getattr(pairing_module, name, None))
        ]
        if available:
            diagnostics.append(
                f"Available pairing callables: {', '.join(sorted(available))}"
            )

    diagnostics.extend(attempts)
    detail = "; ".join(diagnostics)
    if detail:
        detail = f" {detail}"
    raise PairingError(f"Unable to pair with the YouTube app.{detail}")


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

    module = _resolve_pyytlounge()
    api_class = _find_api_class(module)
    if api_class is not None:
        async def pair() -> Any:
            async with api_class("My Remote") as api:
                paired_and_linked = await api.pair(
                    normalized_code
                )  # pairs + links (gets lounge token)
                if not paired_and_linked:
                    raise PairingError(
                        f"YtLoungApi `pair` method failed with {normalized_code}"
                    )
                return api.store_auth_state()  # serialize dict for reuse

        return _normalize_auth_payload(asyncio.run(pair()))

    legacy_result = _pair_via_legacy(module, normalized_code)
    return _normalize_auth_payload(legacy_result)


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
