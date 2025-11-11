"""FastAPI application that exposes a Chromecast remote."""

from __future__ import annotations

import html

import logging

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse

from .casting import (
    CastResult,
    ChromecastUnavailableError,
    cast_youtube_video,
    discover_chromecast_names,
)

logger = logging.getLogger(__name__)

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
      .modal {{ position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; background: rgba(0, 0, 0, 0.4); }}
      .modal.hidden {{ display: none; }}
      .modal-content {{ background: #fff; padding: 1.5rem; border-radius: 0.75rem; width: min(90vw, 22rem); box-shadow: 0 10px 35px rgba(0, 0, 0, 0.2); }}
      .modal-content h2 {{ margin-top: 0; margin-bottom: 0.5rem; }}
      .modal-content p {{ margin-top: 0; color: #444; }}
      #device-list {{ list-style: none; padding: 0; margin: 0 0 1rem; }}
      #device-list li + li {{ margin-top: 0.5rem; }}
      #device-list button {{ width: 100%; padding: 0.5rem 0.75rem; border: 1px solid #ccc; border-radius: 0.5rem; background: #fafafa; font-size: 1rem; cursor: pointer; }}
      #device-list button:hover {{ background: #f0f0f0; }}
      #device-cancel {{ border: none; background: none; color: #0060df; cursor: pointer; font-size: 0.95rem; }}
    </style>
  </head>
  <body>
    <h1>MyTube Remote</h1>
    <p class=\"description\">Kick off the featured recommendation on your Chromecast.</p>
    <ul>
      <li><a id=\"cast-link\" href=\"{cast_url}\">Play the featured video</a></li>
    </ul>
{status_block}
    <div id=\"device-modal\" class=\"modal hidden\" role=\"dialog\" aria-modal=\"true\" aria-labelledby=\"device-modal-title\">
      <div class=\"modal-content\">
        <h2 id=\"device-modal-title\">Choose a device</h2>
        <p>Select a Chromecast device to begin casting.</p>
        <ul id=\"device-list\"></ul>
        <button id=\"device-cancel\" type=\"button\">Cancel</button>
      </div>
    </div>
    <script>
      (() => {{
        const castLink = document.getElementById("cast-link");
        if (!castLink) {{
          return;
        }}

        const modal = document.getElementById("device-modal");
        const deviceList = document.getElementById("device-list");
        const cancelButton = document.getElementById("device-cancel");

        const hideModal = () => {{
          if (!modal) {{
            return;
          }}
          modal.classList.add("hidden");
          if (deviceList) {{
            deviceList.innerHTML = "";
          }}
        }};

        const redirectToCast = (device) => {{
          const url = new URL(castLink.href, window.location.origin);
          if (device) {{
            url.searchParams.set("device", device);
          }}
          window.location.href = url.toString();
        }};

        const showModal = (devices) => {{
          if (!modal || !deviceList) {{
            redirectToCast();
            return;
          }}
          deviceList.innerHTML = "";
          devices.forEach((device) => {{
            const item = document.createElement("li");
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = device;
            button.addEventListener("click", () => {{
              hideModal();
              redirectToCast(device);
            }});
            item.appendChild(button);
            deviceList.appendChild(item);
          }});
          modal.classList.remove("hidden");
        }};

        castLink.addEventListener("click", async (event) => {{
          event.preventDefault();
          try {{
            const response = await fetch("{devices_url}");
            if (!response.ok) {{
              throw new Error("Failed to load devices");
            }}
            const payload = await response.json();
            const devices = Array.isArray(payload.devices) ? payload.devices : [];
            if (devices.length <= 1) {{
              redirectToCast(devices[0]);
              return;
            }}
            showModal(devices);
          }} catch (error) {{
            console.error(error);
            redirectToCast();
          }}
        }});

        if (cancelButton) {{
          cancelButton.addEventListener("click", () => {{
            hideModal();
          }});
        }}

        if (modal) {{
          modal.addEventListener("click", (event) => {{
            if (event.target === modal) {{
              hideModal();
            }}
          }});
        }}
      }})();
    </script>
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
            devices_url=app.url_path_for("list_devices"),
            status_block=status_block,
        )
        return HTMLResponse(html_content)

    @app.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return _render()

    @app.get("/devices")
    async def list_devices() -> dict[str, list[str]]:
        devices = await run_in_threadpool(discover_chromecast_names)
        return {"devices": devices}

    @app.get("/cast", response_class=HTMLResponse)
    async def cast_featured(device: str | None = None) -> HTMLResponse:
        try:
            result: CastResult = await run_in_threadpool(
                cast_youtube_video, YOUTUBE_VIDEO_ID, device_name=device
            )
        except ChromecastUnavailableError as exc:  # pragma: no cover - network hardware required
            logger.warning("Chromecast unavailable: %s", exc)
            return _render(str(exc))

        status = (
            "Casting YouTube video "
            f"https://youtu.be/{result.video_id} to Chromecast '{result.device_name}'."
        )
        logger.info("%s", status)
        return _render(status)

    return app

