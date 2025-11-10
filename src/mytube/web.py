"""Flask application that exposes a Chromecast remote."""

from __future__ import annotations

from flask import Flask, render_template_string, url_for

from .casting import CastResult, ChromecastUnavailableError, cast_youtube_video

YOUTUBE_VIDEO_ID = "CYlon2tvywA"
TEMPLATE = """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <title>MyTube Remote</title>
    <style>
      body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 40rem; }
      h1 { margin-bottom: 0.25rem; }
      p.description { color: #333; margin-top: 0; }
      ul { padding-left: 1.2rem; }
      .status { margin-top: 1.5rem; padding: 1rem; border-radius: 0.5rem; background: #f5f5f5; }
    </style>
  </head>
  <body>
    <h1>MyTube Remote</h1>
    <p class=\"description\">Kick off the featured recommendation on your Chromecast.</p>
    <ul>
      <li><a href=\"{{ cast_url }}\">Play the featured video</a></li>
    </ul>
    {% if status %}
    <p class=\"status\">{{ status }}</p>
    {% endif %}
  </body>
</html>
"""


def create_app() -> Flask:
    """Create a configured Flask application instance."""

    app = Flask(__name__)

    def _render(status: str | None = None) -> str:
        return render_template_string(
            TEMPLATE,
            cast_url=url_for("cast_featured"),
            status=status,
        )

    @app.get("/")
    def home() -> str:
        return _render()

    @app.get("/cast")
    def cast_featured() -> str:
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
