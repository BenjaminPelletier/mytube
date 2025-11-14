"""Unit tests for the YouTube lounge helpers."""

from __future__ import annotations

import asyncio
import types
from dataclasses import dataclass

import pytest

from mytube import ytlounge


def test_pair_with_link_code_uses_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class FakeClient:
        def pair_link_code(self, code: str) -> dict[str, str]:
            captured["code"] = code
            return {"token": code.lower()}

    fake_pairing = types.SimpleNamespace(PairingClient=lambda: FakeClient())
    fake_module = types.SimpleNamespace(pairing=fake_pairing)
    monkeypatch.setattr(ytlounge, "pyytlounge", fake_module)

    result = ytlounge.pair_with_link_code("ABCD-EFGH ")

    assert captured["code"] == "ABCDEFGH"
    assert result == {"token": "abcdefgh"}


def test_pair_with_link_code_uses_top_level_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class FakeClient:
        def pair(self, code: str) -> dict[str, str]:
            captured["code"] = code
            return {"token": code[::-1]}

    fake_module = types.SimpleNamespace(PairingClient=lambda: FakeClient())
    monkeypatch.setattr(ytlounge, "pyytlounge", fake_module)

    result = ytlounge.pair_with_link_code("ABCD")

    assert captured["code"] == "ABCD"
    assert result == {"token": "DCBA"}


def test_pair_with_link_code_converts_dataclass(monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class Payload:
        token: str

    def pair_link_code(code: str) -> Payload:
        return Payload(token=code)

    fake_module = types.SimpleNamespace(pair=pair_link_code)
    monkeypatch.setattr(ytlounge, "pyytlounge", fake_module)

    result = ytlounge.pair_with_link_code("ZXCV")

    assert result == {"token": "ZXCV"}


def test_pair_with_link_code_supports_async(monkeypatch: pytest.MonkeyPatch) -> None:
    async def async_pair(code: str) -> dict[str, str]:
        await asyncio.sleep(0)
        return {"token": code}

    fake_module = types.SimpleNamespace(pair=async_pair)
    monkeypatch.setattr(ytlounge, "pyytlounge", fake_module)

    result = ytlounge.pair_with_link_code("QWER")

    assert result == {"token": "QWER"}


def test_pair_with_link_code_rejects_empty_code(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = types.SimpleNamespace(pair=lambda code: {"token": code})
    monkeypatch.setattr(ytlounge, "pyytlounge", fake_module)

    with pytest.raises(ytlounge.PairingError):
        ytlounge.pair_with_link_code("   ")


def test_pair_with_link_code_reports_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pairing_module = types.SimpleNamespace()
    fake_pairing_module.__name__ = "pyytlounge.pairing"
    fake_pairing_module.PairingClient = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    fake_pairing_module.pair_dummy = lambda code: code

    fake_module = types.SimpleNamespace(pairing=fake_pairing_module, __version__="3.2.0")
    fake_module.__name__ = "pyytlounge"

    monkeypatch.setattr(ytlounge, "pyytlounge", fake_module)

    with pytest.raises(ytlounge.PairingError) as excinfo:
        ytlounge.pair_with_link_code("ABCD")

    message = str(excinfo.value)
    assert "3.2.0" in message
    assert "pair_dummy" in message
    assert "RuntimeError" in message
