"""FastAPI application that exposes a Chromecast remote."""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .casting import (
    CastResult,
    ChromecastUnavailableError,
    cast_youtube_video,
    discover_chromecast_names,
)
from .db import (
    fetch_channel,
    fetch_playlist_items,
    fetch_video,
    initialize_database,
    save_channel,
    save_playlist_items,
    save_video,
    set_resource_label,
)
from .youtube import fetch_youtube_section_data, load_youtube_api_key

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
LIST_TO_RESOURCE_LABEL = {"white": "whitelisted", "black": "blacklisted"}

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


def _channel_resource_content(channel: dict | None) -> str:
    if not channel:
        return (
            "<section>"
            "<h2>Channel Not Found</h2>"
            "<p>No stored data is available for this channel. Submit the identifier "
            "through the form to fetch it from YouTube.</p>"
            "</section>"
        )

    channel_id = html.escape(channel.get("id", ""))
    title = html.escape(channel.get("title") or "Untitled channel")
    description = html.escape(channel.get("description") or "")
    retrieved_at = html.escape(channel.get("retrieved_at") or "Unknown")
    label = channel.get("label")
    if label == "whitelisted":
        vote = "👍"
    elif label == "blacklisted":
        vote = "👎"
    else:
        vote = ""

    description_html = (
        f"<p>{description}</p>" if description else "<p><em>No description provided.</em></p>"
    )

    return (
        "<section>"
        f"<h2>{title} {vote}</h2>"
        f"<p><small>ID: {channel_id}</small></p>"
        f"{description_html}"
        f"<p><strong>Retrieved:</strong> {retrieved_at}</p>"
        "</section>"
    )


def _playlist_resource_content(
    playlist_id: str, playlist_items: list[dict[str, Any]], list_choice: str | None
) -> str:
    escaped_id = html.escape(playlist_id)
    list_summary = (
        f"<p><strong>List preference:</strong> {html.escape(LIST_LABELS[list_choice])}</p>"
        if list_choice in LIST_LABELS
        else ""
    )
    if not playlist_items:
        return (
            "<section>"
            f"<h2>Playlist <code>{escaped_id}</code></h2>"
            f"{list_summary}"
            "<p>No stored playlist items for this playlist.</p>"
            "</section>"
        )

    titles = []
    for item in playlist_items:
        snippet = item.get("snippet") or {}
        title = snippet.get("title") or "Untitled item"
        titles.append(f"<li>{html.escape(title)}</li>")
    items_html = "".join(titles)
    return (
        "<section>"
        f"<h2>Playlist <code>{escaped_id}</code></h2>"
        f"{list_summary}"
        "<p>Stored playlist item titles:</p>"
        f"<ol>{items_html}</ol>"
        "</section>"
    )


def _video_resource_content(video: dict | None) -> str:
    if not video:
        return (
            "<section>"
            "<h2>Video Not Found</h2>"
            "<p>No stored data is available for this video. Submit the identifier "
            "through the form to fetch it from YouTube.</p>"
            "</section>"
        )

    video_id = html.escape(video.get("id", ""))
    title = html.escape(video.get("title") or "Untitled video")
    description = html.escape(video.get("description") or "")
    retrieved_at = html.escape(video.get("retrieved_at") or "Unknown")
    label = video.get("label")
    if label == "whitelisted":
        vote = "👍"
    elif label == "blacklisted":
        vote = "👎"
    else:
        vote = ""

    description_html = (
        f"<p>{description}</p>" if description else "<p><em>No description provided.</em></p>"
    )
    json_payload = html.escape(json.dumps(video.get("raw_json") or {}, indent=2, sort_keys=True))

    return (
        "<section>"
        f"<h2>{title} {vote}</h2>"
        f"<p><small>ID: {video_id}</small></p>"
        f"{description_html}"
        f"<p><strong>Retrieved:</strong> {retrieved_at}</p>"
        f"<h3>YouTube API Response</h3>"
        f"<pre class=\"api-response\">{json_payload}</pre>"
        "</section>"
    )


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

    initialize_database()
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
    ) -> Response:
        normalized_section = _validate_section(section)
        stripped_resource_id = resource_id.strip()
        if not stripped_resource_id:
            raise HTTPException(status_code=400, detail="Resource ID is required")
        if list_choice not in LIST_LABELS:
            raise HTTPException(status_code=400, detail="Unknown list selection")

        api_key = load_youtube_api_key()
        request_url, response_data = await fetch_youtube_section_data(
            normalized_section, stripped_resource_id, api_key
        )
        if normalized_section == "playlists":
            playlist_items = response_data.get("items", [])
            await run_in_threadpool(
                save_playlist_items, stripped_resource_id, playlist_items
            )
            redirect_url = app.url_path_for(
                "view_resource",
                section=normalized_section,
                resource_id=stripped_resource_id,
            )
            if list_choice in LIST_LABELS:
                redirect_url = f"{redirect_url}?list={list_choice}"
            return RedirectResponse(redirect_url, status_code=303)
        if normalized_section == "channels":
            items = response_data.get("items") or []
            if not items:
                raise HTTPException(status_code=404, detail="Channel not found")
            channel_data = items[0]
            channel_id = channel_data.get("id") or stripped_resource_id
            label = LIST_TO_RESOURCE_LABEL.get(list_choice)
            if label is None:
                raise HTTPException(status_code=400, detail="Unknown list selection")

            def _persist_channel() -> None:
                save_channel(channel_data, retrieved_at=datetime.now(timezone.utc))
                set_resource_label("channel", channel_id, label)

            await run_in_threadpool(_persist_channel)
            redirect_url = app.url_path_for(
                "view_resource",
                section=normalized_section,
                resource_id=channel_id,
            )
            return RedirectResponse(redirect_url, status_code=303)

        if normalized_section == "videos":
            items = response_data.get("items") or []
            if not items:
                raise HTTPException(status_code=404, detail="Video not found")
            video_data = items[0]
            video_id = video_data.get("id") or stripped_resource_id
            label = LIST_TO_RESOURCE_LABEL.get(list_choice)
            if label is None:
                raise HTTPException(status_code=400, detail="Unknown list selection")

            def _persist_video() -> None:
                save_video(video_data, retrieved_at=datetime.now(timezone.utc))
                set_resource_label("video", video_id, label)

            await run_in_threadpool(_persist_video)
            redirect_url = app.url_path_for(
                "view_resource",
                section=normalized_section,
                resource_id=video_id,
            )
            return RedirectResponse(redirect_url, status_code=303)

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
        if normalized_section == "playlists":
            playlist_items = await run_in_threadpool(
                fetch_playlist_items, resource_id
            )
            content = _playlist_resource_content(resource_id, playlist_items, list)
        elif normalized_section == "channels":
            channel = await run_in_threadpool(fetch_channel, resource_id)
            content = _channel_resource_content(channel)
        elif normalized_section == "videos":
            video = await run_in_threadpool(fetch_video, resource_id)
            content = _video_resource_content(video)
        else:
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

