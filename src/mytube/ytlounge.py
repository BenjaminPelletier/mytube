"""Utilities for pairing with the YouTube TV lounge API."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Iterable

try:  # pragma: no cover - optional dependency
    import pyytlounge  # type: ignore import
except ModuleNotFoundError:  # pragma: no cover - tests provide a stub
    pyytlounge = None  # type: ignore[assignment]

__all__ = ["PairingError", "pair_with_link_code"]


logger = logging.getLogger(__name__)


class PairingError(RuntimeError):
    """Raised when pairing with the YouTube app fails."""


_PAIRING_CALLABLE_NAMES = (
    "pair_link_code",
    "pair_code",
    "pair",
    "pair_with_code",
)


def pair_with_link_code(link_code: str) -> dict[str, Any]:
    """Pair with the YouTube TV app using a "Link with TV" code."""

    normalized_code = _normalize_code(link_code)
    if not normalized_code:
        raise PairingError("Enter the full Link with TV code from your TV.")

    module = pyytlounge
    if module is None:
        raise PairingError("pyytlounge is not available to complete pairing.")

    api_class = getattr(module, "YtLoungeApi", None)
    if api_class is None:
        wrapper = getattr(module, "wrapper", None)
        if wrapper is not None:
            api_class = getattr(wrapper, "YtLoungeApi", None)

    if api_class is not None:
        async def _pair() -> Any:
            async with api_class("My Remote") as api:  # type: ignore[call-arg]
                paired = await api.pair(normalized_code)
                if not paired:
                    raise PairingError("Unable to pair with the YouTube app.")
                return api.store_auth_state()

        return _normalize_auth_payload(asyncio.run(_pair()))

    attempts: list[str] = []
    for func, description in _collect_pairing_callables(module, attempts):
        try:
            result = _call_pairing(func, normalized_code)
        except Exception as exc:  # pragma: no cover - diagnostics only
            attempts.append(f"{description} raised {exc.__class__.__name__}: {exc}")
            continue
        return _normalize_auth_payload(result)

    diagnostics: list[str] = []
    version = getattr(module, "__version__", None)
    if version:
        diagnostics.append(f"pyytlounge version {version}")

    pairing_module = getattr(module, "pairing", None)
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


def _collect_pairing_callables(
    module: Any, attempts: list[str]
) -> Iterable[tuple[Callable[[str], Any], str]]:
    pairing_module = getattr(module, "pairing", None)
    if pairing_module is not None:
        client_factory = getattr(pairing_module, "PairingClient", None)
        if callable(client_factory):
            try:
                client = client_factory()
            except Exception as exc:  # pragma: no cover - diagnostics only
                attempts.append(
                    f"{_object_name(pairing_module)}.PairingClient raised {exc.__class__.__name__}: {exc}"
                )
            else:
                for name in _PAIRING_CALLABLE_NAMES:
                    func = getattr(client, name, None)
                    if callable(func):
                        yield func, f"{_object_name(pairing_module)}.PairingClient.{name}"
        for name in _PAIRING_CALLABLE_NAMES:
            func = getattr(pairing_module, name, None)
            if callable(func):
                yield func, f"{_object_name(pairing_module)}.{name}"

    client_factory = getattr(module, "PairingClient", None)
    if callable(client_factory):
        try:
            client = client_factory()
        except Exception as exc:  # pragma: no cover - diagnostics only
            attempts.append(
                f"{_object_name(module)}.PairingClient raised {exc.__class__.__name__}: {exc}"
            )
        else:
            for name in _PAIRING_CALLABLE_NAMES:
                func = getattr(client, name, None)
                if callable(func):
                    yield func, f"{_object_name(module)}.PairingClient.{name}"

    for name in _PAIRING_CALLABLE_NAMES:
        func = getattr(module, name, None)
        if callable(func):
            yield func, f"{_object_name(module)}.{name}"


def _call_pairing(func: Callable[[str], Any], code: str) -> Any:
    result = func(code)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _object_name(obj: Any) -> str:
    if hasattr(obj, "__name__"):
        return getattr(obj, "__name__")  # type: ignore[return-value]
    return obj.__class__.__name__


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
