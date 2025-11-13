"""FastAPI application that exposes a Chromecast remote."""

from __future__ import annotations

import html
import json
import logging
import os
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse

from .casting import (
    CastResult,
    ChromecastUnavailableError,
    cast_youtube_video,
    discover_chromecast_names,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
static_directory = BASE_DIR / "static"

YOUTUBE_VIDEO_ID = "CYlon2tvywA"

CONFIG_NAVIGATION = (
    ("channels", "Channels"),
    ("playlists", "Playlists"),
    ("videos", "Videos"),
)
CONFIG_LABELS = {slug: label for slug, label in CONFIG_NAVIGATION}

LIST_LABELS = {"white": "Whitelist", "black": "Blacklist"}

HARD_CODED_RESOURCES = {
    "channels": {
        "UC_x5XG1OV2P6uZZ5FSM9Ttw": {
            "title": "Google Developers",
            "description": "The Google Developers channel, packed with product news and videos.",
            "subscribers": "2.6M subscribers",
        },
        "UCVHFbqXqoYvEWM1Ddxl0QDg": {
            "title": "Python",
            "description": "Official Python Software Foundation updates and tutorials.",
            "subscribers": "600K subscribers",
        },
    },
    "playlists": {
        "PL590L5WQmH8fJ54FqA0bG8TPEj-UZ_J1b": {
            "title": "YouTube Developers Live",
            "description": "Sessions exploring the YouTube API and developer tooling.",
            "items": "25 videos",
        },
        "PLAwxTw4SYaPnMwH8bJ5rE2l78G_uuNzhf": {
            "title": "FastAPI Tutorials",
            "description": "A curated list of introductory FastAPI tutorials.",
            "items": "12 videos",
        },
    },
    "videos": {
        "CYlon2tvywA": {
            "title": "Introducing MyTube",
            "description": "An overview of the MyTube Chromecast remote prototype.",
            "duration": "9 minutes",
        },
        "kLhFQnK0HXg": {
            "title": "FastAPI in 15 Minutes",
            "description": "A rapid introduction to building web apps with FastAPI.",
            "duration": "15 minutes",
        },
    },
}


def _validate_section(section: str) -> str:
    normalized = section.lower()
    if normalized not in CONFIG_LABELS:
        raise HTTPException(status_code=404, detail="Unknown configuration section")
    return normalized


def _navigation_links(app: FastAPI, active_section: str | None) -> list[dict[str, str | bool]]:
    links: list[dict[str, str | bool]] = []
    for slug, label in CONFIG_NAVIGATION:
        links.append(
            {
                "url": app.url_path_for("configure_section", section=slug),
                "label": label,
                "active": active_section == slug,
            }
        )
    return links


def _render_config_page(
    request: Request,
    app: FastAPI,
    *,
    heading: str,
    active_section: str | None,
    content: str,
    form_action: str | None = None,
    resource_value: str = "",
) -> HTMLResponse:
    navigation = _navigation_links(app, active_section)
    action = form_action or app.url_path_for(
        "create_resource", section=active_section or CONFIG_NAVIGATION[0][0]
    )
    return templates.TemplateResponse(
        "configure.html",
        {
            "request": request,
            "heading": heading,
            "navigation": navigation,
            "form_action": action,
            "resource_value": resource_value,
            "content_html": content,
        },
    )


def _section_overview_content(section: str) -> str:
    resources = HARD_CODED_RESOURCES.get(section, {})
    if not resources:
        return "<section><p>No example resources available.</p></section>"
    items = []
    for resource_id, details in resources.items():
        escaped_id = html.escape(resource_id)
        title = html.escape(details.get("title", "Unnamed"))
        list_items = [f"<dt>ID</dt><dd>{escaped_id}</dd>"]
        for key, value in details.items():
            escaped_key = html.escape(key.replace("_", " ").title())
            escaped_value = html.escape(value)
            list_items.append(f"<dt>{escaped_key}</dt><dd>{escaped_value}</dd>")
        detail_list = "".join(list_items)
        items.append(
            "<section>"
            f"<h2>{title}</h2>"
            f"<p>Preview of stored metadata for <code>{escaped_id}</code>.</p>"
            f"<dl>{detail_list}</dl>"
            "</section>"
        )
    return "".join(items)


def _resource_detail_content(section: str, resource_id: str, list_choice: str | None) -> str:
    resources = HARD_CODED_RESOURCES.get(section, {})
    resource = resources.get(resource_id)
    escaped_id = html.escape(resource_id)
    label = CONFIG_LABELS.get(section, section.title())
    list_summary = (
        f"<p><strong>List preference:</strong> {html.escape(LIST_LABELS[list_choice])}</p>"
        if list_choice in LIST_LABELS
        else ""
    )
    if not resource:
        return (
            "<section>"
            f"<h2>New {html.escape(label)} Resource</h2>"
            f"<p>No stored metadata for <code>{escaped_id}</code> yet."
            f"{list_summary}"
            "<p>This demo view uses hard-coded data. Persisted storage will be added later.</p>"
            "</section>"
        )

    details = [f"<dt>ID</dt><dd>{escaped_id}</dd>"]
    for key, value in resource.items():
        details.append(
            f"<dt>{html.escape(key.replace('_', ' ').title())}</dt><dd>{html.escape(value)}</dd>"
        )
    detail_list = "".join(details)
    title = html.escape(resource.get("title", f"{label} Resource"))
    return (
        "<section>"
        f"<h2>{title}</h2>"
        f"<p>Preview data for <code>{escaped_id}</code> in the configuration workspace.</p>"
        f"{list_summary}"
        f"<dl>{detail_list}</dl>"
        "</section>"
    )


def _load_youtube_api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY")
    if key:
        stripped = key.strip()
        if stripped:
            return stripped

    key_path = Path.cwd() / ".youtube-api"
    if key_path.exists():
        file_key = key_path.read_text(encoding="utf-8").strip()
        if file_key:
            return file_key

    raise HTTPException(status_code=500, detail="YouTube API key is not configured")


def _youtube_api_request(endpoint: str, params: dict[str, str]) -> tuple[str, dict[str, Any]]:
    query = urllib.parse.urlencode(params)
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{query}"
    try:
        with urllib.request.urlopen(url) as response:
            charset = response.headers.get_content_charset("utf-8")
            payload = response.read().decode(charset)
    except urllib.error.HTTPError as exc:  # pragma: no cover - network response paths
        try:
            error_body = exc.read().decode("utf-8", "ignore")
        except Exception:  # pragma: no cover - defensive
            error_body = ""
        detail = error_body or exc.reason
        raise HTTPException(status_code=exc.code, detail=f"YouTube API error: {detail}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network response paths
        raise HTTPException(
            status_code=502, detail=f"Failed to contact YouTube API: {exc.reason}"
        ) from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=502, detail="Invalid response from YouTube API") from exc
    return url, data


async def _fetch_youtube_section_data(
    section: str, resource_id: str, api_key: str
) -> tuple[str, dict[str, Any]]:
    if section == "playlists":
        params = {
            "part": "snippet,contentDetails",
            "playlistId": resource_id,
            "maxResults": "50",
            "key": api_key,
        }
        endpoint = "playlistItems"
    elif section == "channels":
        params = {
            "part": "snippet,statistics",
            "key": api_key,
        }
        if resource_id.startswith("@"):  # Handle input
            params["forHandle"] = resource_id[1:]
        else:
            params["id"] = resource_id
        endpoint = "channels"
    elif section == "videos":
        params = {
            "part": "snippet,contentDetails,statistics",
            "id": resource_id,
            "key": api_key,
        }
        endpoint = "videos"
    else:  # pragma: no cover - unreachable due to validation
        raise HTTPException(status_code=400, detail="Unsupported section for API lookup")

    return await run_in_threadpool(_youtube_api_request, endpoint, params)


def _api_response_content(
    resource_id: str,
    list_choice: str,
    request_url: str,
    response_data: dict[str, Any],
) -> str:
    escaped_id = html.escape(resource_id)
    json_payload = html.escape(json.dumps(response_data, indent=2, sort_keys=True))
    list_summary = (
        f"<p><strong>List preference:</strong> {html.escape(LIST_LABELS[list_choice])}</p>"
        if list_choice in LIST_LABELS
        else ""
    )
    return (
        "<section>"
        f"<h2>YouTube API response for <code>{escaped_id}</code></h2>"
        f"{list_summary}"
        f"<p><strong>Endpoint:</strong> {html.escape(request_url)}</p>"
        f"<pre class=\"api-response\">{json_payload}</pre>"
        "</section>"
    )


def create_app() -> FastAPI:
    """Create a configured FastAPI application instance."""

    app = FastAPI(title="MyTube Remote")

    if static_directory.exists():
        app.mount("/static", StaticFiles(directory=str(static_directory)), name="static")

    def _render(request: Request, status: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            "home.html",
            {
                "request": request,
                "cast_url": app.url_path_for("cast_featured"),
                "devices_url": app.url_path_for("list_devices"),
                "status": status,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        return _render(request)

    @app.get("/configure", response_class=HTMLResponse)
    async def configure_home(request: Request) -> HTMLResponse:
        content = (
            "<section>"
            "<h2>Welcome to the configuration workspace</h2>"
            "<p>Use the menu above to manage channels, playlists, or videos."
            " Enter a YouTube identifier and choose whether it belongs to the whitelist"
            " or blacklist to preview how the resource will appear.</p>"
            "</section>"
        )
        return _render_config_page(
            request,
            app,
            heading="Configure MyTube",
            active_section=None,
            content=content,
            form_action=app.url_path_for("create_resource", section="channels"),
        )

    @app.get("/configure/{section}", response_class=HTMLResponse, name="configure_section")
    async def configure_section(request: Request, section: str) -> HTMLResponse:
        normalized_section = _validate_section(section)
        content = _section_overview_content(normalized_section)
        heading = f"Manage {CONFIG_LABELS[normalized_section]}"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section=normalized_section,
            content=content,
            form_action=app.url_path_for("create_resource", section=normalized_section),
        )

    @app.post("/configure/{section}", name="create_resource")
    async def create_resource(
        request: Request,
        section: str,
        resource_id: str = Form(..., alias="resource_id"),
        list_choice: str = Form(..., alias="list"),
    ) -> HTMLResponse:
        normalized_section = _validate_section(section)
        stripped_resource_id = resource_id.strip()
        if not stripped_resource_id:
            raise HTTPException(status_code=400, detail="Resource ID is required")
        if list_choice not in LIST_LABELS:
            raise HTTPException(status_code=400, detail="Unknown list selection")

        api_key = _load_youtube_api_key()
        request_url, response_data = await _fetch_youtube_section_data(
            normalized_section, stripped_resource_id, api_key
        )
        content = _api_response_content(
            stripped_resource_id, list_choice, request_url, response_data
        )
        heading = f"{CONFIG_LABELS[normalized_section]} API Preview"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section=normalized_section,
            content=content,
            form_action=app.url_path_for("create_resource", section=normalized_section),
            resource_value=stripped_resource_id,
        )

    @app.get(
        "/configure/{section}/{resource_id}",
        response_class=HTMLResponse,
        name="view_resource",
    )
    async def view_resource(
        request: Request,
        section: str,
        resource_id: str,
        list: str | None = None,
    ) -> HTMLResponse:
        normalized_section = _validate_section(section)
        content = _resource_detail_content(normalized_section, resource_id, list)
        heading = f"{CONFIG_LABELS[normalized_section]} Resource"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section=normalized_section,
            content=content,
            form_action=app.url_path_for("create_resource", section=normalized_section),
            resource_value=resource_id,
        )

    @app.get("/devices")
    async def list_devices() -> dict[str, list[str]]:
        devices = await run_in_threadpool(discover_chromecast_names)
        return {"devices": devices}

    @app.get("/cast", response_class=HTMLResponse)
    async def cast_featured(request: Request, device: str | None = None) -> HTMLResponse:
        try:
            result: CastResult = await run_in_threadpool(
                cast_youtube_video, YOUTUBE_VIDEO_ID, device_name=device
            )
        except ChromecastUnavailableError as exc:  # pragma: no cover - network hardware required
            logger.warning("Chromecast unavailable: %s", exc)
            return _render(request, str(exc))

        status = (
            "Casting YouTube video "
            f"https://youtu.be/{result.video_id} to Chromecast '{result.device_name}'."
        )
        logger.info("%s", status)
        return _render(request, status)

    return app

