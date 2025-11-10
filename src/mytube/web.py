"""FastAPI application that exposes a Chromecast remote."""

from __future__ import annotations

import html

from fastapi import FastAPI
from starlette.responses import HTMLResponse

from .casting import CastResult, ChromecastUnavailableError, cast_youtube_video

YOUTUBE_VIDEO_ID = "CYlon2tvywA"
TEMPLATE = """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <title>MyTube Remote</title>
    <style>
      body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 40rem; }}
      h1 {{ margin-bottom: 0.25rem; }}
      p.description {{ color: #333; margin-top: 0; }}
      ul {{ padding-left: 1.2rem; }}
      .status {{ margin-top: 1.5rem; padding: 1rem; border-radius: 0.5rem; background: #f5f5f5; }}
    </style>
  </head>
  <body>
    <h1>MyTube Remote</h1>
    <p class=\"description\">Kick off the featured recommendation on your Chromecast.</p>
    <ul>
      <li><a href=\"{cast_url}\">Play the featured video</a></li>
    </ul>
{status_block}
  </body>
</html>
"""


def create_app() -> FastAPI:
    """Create a configured FastAPI application instance."""

    app = FastAPI(title="MyTube Remote")

    def _render(status: str | None = None) -> HTMLResponse:
        status_block = ""
        if status:
            safe_status = html.escape(status)
            status_block = f"    <p class=\"status\">{safe_status}</p>\n"

        html_content = TEMPLATE.format(
            cast_url=app.url_path_for("cast_featured"),
            status_block=status_block,
        )
        return HTMLResponse(html_content)

    @app.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return _render()

    @app.get("/cast", response_class=HTMLResponse)
    async def cast_featured() -> HTMLResponse:
        try:
            result: CastResult = cast_youtube_video(YOUTUBE_VIDEO_ID)
        except ChromecastUnavailableError as exc:  # pragma: no cover - network hardware required
            return _render(str(exc))

        status = (
            "Casting YouTube video "
            f"https://youtu.be/{result.video_id} to Chromecast '{result.device_name}'."
        )
        return _render(status)

    return app
