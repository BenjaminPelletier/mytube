import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("sqlmodel")

from mytube.lounge import LoungeController


def _auth_payload() -> dict[str, str | None]:
    return {
        "version": 0,
        "screenId": "screen-123",
        "loungeIdToken": "token-456",
        "refreshToken": None,
        "expiry": None,
    }


@pytest.mark.asyncio
async def test_play_video_invokes_underlying_api(monkeypatch):
    played: list[str] = []

    class _StubApi:
        def __init__(self, name: str):
            self._name = name
            self._connected = False
            self._auth = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):  # pragma: no cover - cleanup
            await self.close()
            return False

        def load_auth_state(self, payload):
            self._auth = dict(payload)

        async def connect(self):
            self._connected = True
            return True

        async def refresh_auth(self):  # pragma: no cover - unused in test
            self._connected = True
            return True

        def connected(self):
            return self._connected

        async def close(self):
            self._connected = False

        def play_video(self, video_id: str):
            played.append(video_id)

    monkeypatch.setattr("mytube.lounge.YtLoungeApi", _StubApi)

    controller = LoungeController("Test", _auth_payload())

    await controller.play_video("abc123")

    assert played == ["abc123"]


@pytest.mark.asyncio
async def test_play_video_requires_identifier(monkeypatch):
    monkeypatch.setattr("mytube.lounge.YtLoungeApi", lambda name: None)
    controller = LoungeController("Test", _auth_payload())

    with pytest.raises(ValueError):
        await controller.play_video("")
