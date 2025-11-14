from __future__ import annotations

import sys
import types
from pathlib import Path


def _install_fastapi_stubs() -> None:
    if "fastapi" in sys.modules:
        return

    fastapi_module = types.ModuleType("fastapi")

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class _FastAPI:
        def __init__(self, *args, **kwargs):
            self.routes: list[tuple[str, str, callable]] = []

        def mount(self, *args, **kwargs) -> None:  # pragma: no cover - stub
            return None

        def url_path_for(self, *args, **kwargs) -> str:  # pragma: no cover - stub
            return "/"

        def get(self, *args, **kwargs):  # pragma: no cover - stub
            def decorator(func):
                return func

            return decorator

        def post(self, *args, **kwargs):  # pragma: no cover - stub
            def decorator(func):
                return func

            return decorator

    class _Request:  # pragma: no cover - stub
        pass

    fastapi_module.FastAPI = _FastAPI
    fastapi_module.Form = lambda *args, **kwargs: None  # pragma: no cover - stub
    fastapi_module.HTTPException = _HTTPException
    fastapi_module.Query = lambda *args, **kwargs: None  # pragma: no cover - stub
    fastapi_module.Request = _Request

    staticfiles_module = types.ModuleType("fastapi.staticfiles")

    class _StaticFiles:  # pragma: no cover - stub
        def __init__(self, *args, **kwargs):
            pass

    staticfiles_module.StaticFiles = _StaticFiles

    templating_module = types.ModuleType("fastapi.templating")

    class _TemplateResponse:  # pragma: no cover - stub
        pass

    class _Templates:  # pragma: no cover - stub
        def __init__(self, *args, **kwargs):
            pass

        def TemplateResponse(self, *args, **kwargs):
            return _TemplateResponse()

    templating_module.Jinja2Templates = _Templates

    responses_module = types.ModuleType("starlette.responses")

    class _Response:  # pragma: no cover - stub
        def __init__(self, *args, **kwargs):
            pass

    class _HTMLResponse(_Response):  # pragma: no cover - stub
        pass

    class _RedirectResponse(_Response):  # pragma: no cover - stub
        pass

    responses_module.Response = _Response
    responses_module.HTMLResponse = _HTMLResponse
    responses_module.RedirectResponse = _RedirectResponse

    concurrency_module = types.ModuleType("starlette.concurrency")

    async def _run_in_threadpool(func, *args, **kwargs):  # pragma: no cover - stub
        return func(*args, **kwargs)

    concurrency_module.run_in_threadpool = _run_in_threadpool

    sys.modules["fastapi"] = fastapi_module
    sys.modules["fastapi.staticfiles"] = staticfiles_module
    sys.modules["fastapi.templating"] = templating_module
    sys.modules["starlette.responses"] = responses_module
    sys.modules["starlette.concurrency"] = concurrency_module

    if "pychromecast" not in sys.modules:
        pychromecast_module = types.ModuleType("pychromecast")

        class _Chromecast:  # pragma: no cover - stub
            def __init__(self, name: str = "Stub"):
                self.name = name

            def wait(self) -> None:
                return None

            def register_handler(self, *args, **kwargs) -> None:
                return None

        def _get_chromecasts():  # pragma: no cover - stub
            return [], None

        pychromecast_module.Chromecast = _Chromecast
        pychromecast_module.get_chromecasts = _get_chromecasts

        controllers_module = types.ModuleType("pychromecast.controllers")
        youtube_module = types.ModuleType("pychromecast.controllers.youtube")

        class _YouTubeController:  # pragma: no cover - stub
            def __init__(self):
                pass

            def play_video(self, *args, **kwargs) -> None:
                return None

        youtube_module.YouTubeController = _YouTubeController

        sys.modules["pychromecast"] = pychromecast_module
        sys.modules["pychromecast.controllers"] = controllers_module
        sys.modules["pychromecast.controllers.youtube"] = youtube_module

    if "pyytlounge" not in sys.modules:
        pyytlounge_module = types.ModuleType("pyytlounge")

        class _PairingClient:  # pragma: no cover - stub
            def pair_link_code(self, code: str):
                return {"paired": code}

        pairing_module = types.ModuleType("pyytlounge.pairing")
        pairing_module.PairingClient = _PairingClient

        def _pair_link_code(code: str):  # pragma: no cover - stub
            return {"paired": code}

        pairing_module.pair_link_code = _pair_link_code

        exceptions_module = types.ModuleType("pyytlounge.exceptions")

        class _LoungeError(RuntimeError):  # pragma: no cover - stub
            pass

        class _NotConnected(_LoungeError):  # pragma: no cover - stub
            pass

        class _NotLinked(_LoungeError):  # pragma: no cover - stub
            pass

        class _NotPaired(_LoungeError):  # pragma: no cover - stub
            pass

        exceptions_module.NotConnectedException = _NotConnected
        exceptions_module.NotLinkedException = _NotLinked
        exceptions_module.NotPairedException = _NotPaired

        wrapper_module = types.ModuleType("pyytlounge.wrapper")
        models_module = types.ModuleType("pyytlounge.models")

        class _YtLoungeApi:  # pragma: no cover - stub
            def __init__(self, name: str):
                self._name = name
                self._connected = False
                self._auth_payload: dict[str, str] | None = None

            async def __aenter__(self):  # pragma: no cover - stub
                return self

            async def __aexit__(self, exc_type, exc, tb):  # pragma: no cover - stub
                await self.close()
                return False

            def load_auth_state(self, payload):  # pragma: no cover - stub
                self._auth_payload = dict(payload)

            async def connect(self):  # pragma: no cover - stub
                self._connected = True
                return True

            async def refresh_auth(self):  # pragma: no cover - stub
                self._connected = True
                return True

            def connected(self):  # pragma: no cover - stub
                return self._connected

            async def pair(self, code: str):  # pragma: no cover - stub
                self._auth_payload = {"paired": code}
                return True

            def store_auth_state(self):  # pragma: no cover - stub
                return self._auth_payload or {}

            async def close(self):  # pragma: no cover - stub
                self._connected = False

        wrapper_module.YtLoungeApi = _YtLoungeApi
        models_module.AUTH_VERSION_V1 = 0
        models_module.CURRENT_AUTH_VERSION = 0

        pyytlounge_module.pairing = pairing_module
        pyytlounge_module.YtLoungeApi = _YtLoungeApi
        pyytlounge_module.models = models_module

        sys.modules["pyytlounge"] = pyytlounge_module
        sys.modules["pyytlounge.pairing"] = pairing_module
        sys.modules["pyytlounge.exceptions"] = exceptions_module
        sys.modules["pyytlounge.wrapper"] = wrapper_module
        sys.modules["pyytlounge.models"] = models_module


_install_fastapi_stubs()

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
