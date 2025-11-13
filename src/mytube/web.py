"""FastAPI application that exposes a Chromecast remote."""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
    fetch_all_channels,
    fetch_all_playlists,
    fetch_all_videos,
    fetch_channel,
    fetch_channel_sections,
    fetch_playlist,
    fetch_playlist_items,
    fetch_video,
    initialize_database,
    save_channel,
    save_channel_sections,
    save_playlist,
    save_playlist_items,
    save_video,
    set_resource_label,
)
from .youtube import (
    fetch_youtube_playlist,
    fetch_youtube_channel_sections,
    fetch_youtube_playlists,
    fetch_youtube_section_data,
    load_youtube_api_key,
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
LIST_TO_RESOURCE_LABEL = {"white": "whitelisted", "black": "blacklisted"}

CONFIG_ROUTE_NAMES = {
    "channels": "configure_channels",
    "playlists": "configure_playlists",
    "videos": "configure_videos",
}

CREATE_ROUTE_NAMES = {
    "channels": "create_channels",
    "playlists": "create_playlists",
    "videos": "create_videos",
}


def _validate_section(section: str) -> str:
    normalized = section.lower()
    if normalized not in CONFIG_LABELS:
        raise HTTPException(status_code=404, detail="Unknown configuration section")
    return normalized


def _navigation_links(app: FastAPI, active_section: str | None) -> list[dict[str, str | bool]]:
    links: list[dict[str, str | bool]] = []
    for slug, label in CONFIG_NAVIGATION:
        route_name = CONFIG_ROUTE_NAMES[slug]
        links.append(
            {
                "url": app.url_path_for(route_name),
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
    default_section = active_section or CONFIG_NAVIGATION[0][0]
    default_route = CREATE_ROUTE_NAMES[default_section]
    action = form_action or app.url_path_for(default_route)
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


def _resource_vote(label: str | None) -> str:
    if label == "whitelisted":
        return "👍"
    if label == "blacklisted":
        return "👎"
    return ""


def _channels_overview_content(channels: list[dict[str, Any]]) -> str:
    if not channels:
        return (
            "<section>"
            "<h2>Stored Channels</h2>"
            "<p>No channels have been stored yet.</p>"
            "</section>"
        )

    items = []
    for channel in channels:
        channel_id = channel.get("id") or ""
        if not channel_id:
            continue
        title = channel.get("title") or channel_id
        retrieved_at = channel.get("retrieved_at") or "Unknown"
        vote = _resource_vote(channel.get("label"))
        encoded_id = quote(channel_id, safe="")
        link = f"<a href=\"/configure/channels/{encoded_id}\">{html.escape(title)}</a>"
        vote_display = f" {vote}" if vote else ""
        items.append(
            "<li>"
            f"{link}{vote_display}"
            f"<br><small>Retrieved: {html.escape(retrieved_at)}</small>"
            "</li>"
        )

    if not items:
        return (
            "<section>"
            "<h2>Stored Channels</h2>"
            "<p>No channels have been stored yet.</p>"
            "</section>"
        )

    items_html = "".join(items)
    return (
        "<section>"
        "<h2>Stored Channels</h2>"
        "<ol>"
        f"{items_html}"
        "</ol>"
        "</section>"
    )


def _videos_overview_content(videos: list[dict[str, Any]]) -> str:
    if not videos:
        return (
            "<section>"
            "<h2>Stored Videos</h2>"
            "<p>No videos have been stored yet.</p>"
            "</section>"
        )

    items = []
    for video in videos:
        video_id = video.get("id") or ""
        if not video_id:
            continue
        title = video.get("title") or video_id
        retrieved_at = video.get("retrieved_at") or "Unknown"
        vote = _resource_vote(video.get("label"))
        encoded_id = quote(video_id, safe="")
        link = f"<a href=\"/configure/videos/{encoded_id}\">{html.escape(title)}</a>"
        vote_display = f" {vote}" if vote else ""
        items.append(
            "<li>"
            f"{link}{vote_display}"
            f"<br><small>Retrieved: {html.escape(retrieved_at)}</small>"
            "</li>"
        )

    if not items:
        return (
            "<section>"
            "<h2>Stored Videos</h2>"
            "<p>No videos have been stored yet.</p>"
            "</section>"
        )

    items_html = "".join(items)
    return (
        "<section>"
        "<h2>Stored Videos</h2>"
        "<ol>"
        f"{items_html}"
        "</ol>"
        "</section>"
    )


def _playlists_overview_content(playlists: list[dict[str, Any]]) -> str:
    if not playlists:
        return (
            "<section>"
            "<h2>Stored Playlists</h2>"
            "<p>No playlists have been stored yet.</p>"
            "</section>"
        )

    items: list[str] = []
    for playlist in playlists:
        playlist_id = playlist.get("id") or ""
        if not playlist_id:
            continue

        title = playlist.get("title") or playlist_id
        vote = _resource_vote(playlist.get("label"))
        encoded_playlist_id = quote(playlist_id, safe="")
        playlist_link = (
            f"<a href=\"/configure/playlists/{encoded_playlist_id}\">{html.escape(title)}</a>"
        )

        channel_id = playlist.get("channel_id") or ""
        channel_title = playlist.get("channel_title") or channel_id or "Unknown channel"
        if channel_id:
            encoded_channel_id = quote(channel_id, safe="")
            channel_display = (
                f"<a href=\"/configure/channels/{encoded_channel_id}\">"
                f"{html.escape(channel_title)}"
                "</a>"
            )
        else:
            channel_display = html.escape(channel_title)

        vote_display = f" {vote}" if vote else ""
        items.append(
            f"<li>[{channel_display}] {playlist_link}{vote_display}</li>"
        )

    if not items:
        return (
            "<section>"
            "<h2>Stored Playlists</h2>"
            "<p>No playlists have been stored yet.</p>"
            "</section>"
        )

    items_html = "".join(items)
    return (
        "<section>"
        "<h2>Stored Playlists</h2>"
        "<ol>"
        f"{items_html}"
        "</ol>"
        "</section>"
    )


def _channel_resource_content(channel: dict | None, sections: list[dict]) -> str:
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

    if sections:
        section_items: list[str] = []
        for section in sections:
            title_text = section.get("title")
            if not title_text:
                raw = section.get("raw_json") or {}
                snippet = raw.get("snippet") if isinstance(raw, dict) else {}
                title_text = (snippet or {}).get("title") or section.get("id") or "Untitled"
            section_items.append(f"<li>{html.escape(title_text)}</li>")
        sections_html = (
            "<h3>Channel Sections</h3>"
            "<ol>"
            f"{''.join(section_items)}"
            "</ol>"
        )
    else:
        sections_html = (
            "<h3>Channel Sections</h3>"
            "<p><em>No channel sections stored.</em></p>"
        )

    return (
        "<section>"
        f"<h2>{title} {vote}</h2>"
        f"<p><small>ID: {channel_id}</small></p>"
        f"{description_html}"
        f"<p><strong>Retrieved:</strong> {retrieved_at}</p>"
        f"{sections_html}"
        "</section>"
    )


def _playlist_resource_content(
    playlist_id: str,
    playlist_items: list[dict[str, Any]],
    list_choice: str | None,
    playlist: dict | None,
) -> str:
    escaped_id = html.escape(playlist_id)
    if playlist:
        title = html.escape(playlist.get("title") or playlist_id)
        raw_data = playlist.get("raw_json") if isinstance(playlist, dict) else None
        snippet = raw_data.get("snippet") if isinstance(raw_data, dict) else {}
        channel_id = snippet.get("channelId") if isinstance(snippet, dict) else None
        channel_title = snippet.get("channelTitle") if isinstance(snippet, dict) else None
        channel_line = ""
        if channel_id or channel_title:
            channel_title_text = html.escape(channel_title or channel_id or "")
            if channel_id:
                encoded_channel_id = quote(channel_id, safe="")
                channel_display = (
                    f"<a href=\"/configure/channels/{encoded_channel_id}\">{channel_title_text}</a>"
                )
            else:
                channel_display = channel_title_text
            channel_line = f"<p>{channel_display}</p>"
        heading = (
            f"<h2>{title}</h2>"
            f"{channel_line}"
            f"<p><small>ID: {escaped_id}</small></p>"
        )
    else:
        heading = f"<h2>Playlist <code>{escaped_id}</code></h2>"
    list_summary = (
        f"<p><strong>List preference:</strong> {html.escape(LIST_LABELS[list_choice])}</p>"
        if list_choice in LIST_LABELS
        else ""
    )
    if not playlist_items:
        return (
            "<section>"
            f"{heading}"
            f"{list_summary}"
            "<p>No stored playlist items for this playlist.</p>"
            "</section>"
        )

    titles = []
    for item in playlist_items:
        snippet = item.get("snippet") or {}
        title = snippet.get("title") or "Untitled item"

        video_id = None
        resource = snippet.get("resourceId") if isinstance(snippet, dict) else None
        if isinstance(resource, dict):
            if resource.get("kind") == "youtube#video":
                video_id = resource.get("videoId")
            video_id = video_id or resource.get("videoId")
        if not video_id:
            content_details = item.get("contentDetails")
            if isinstance(content_details, dict):
                video_id = content_details.get("videoId")

        title_text = html.escape(title)
        if video_id:
            encoded_video_id = quote(str(video_id), safe="")
            title_text = (
                f"<a href=\"/configure/videos/{encoded_video_id}\">{title_text}</a>"
            )

        titles.append(f"<li>{title_text}</li>")
    items_html = "".join(titles)
    return (
        "<section>"
        f"{heading}"
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
            form_action=app.url_path_for(CREATE_ROUTE_NAMES["channels"]),
        )

    async def _create_resource(
        request: Request,
        section: str,
        resource_id: str,
        list_choice: str,
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
            _, playlist_metadata = await fetch_youtube_playlist(
                stripped_resource_id, api_key
            )
            playlist_data = (playlist_metadata.get("items") or [None])[0]
            if playlist_data:
                now = datetime.now(timezone.utc)

                def _persist_playlist() -> None:
                    save_playlist(playlist_data, retrieved_at=now)

                await run_in_threadpool(_persist_playlist)
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

            sections_items: list[dict[str, Any]] = []
            if channel_data.get("id"):
                _, sections_data = await fetch_youtube_channel_sections(
                    channel_data["id"], api_key
                )
                sections_items = sections_data.get("items") or []
            playlist_ids: list[str] = []
            playlist_id_set: set[str] = set()

            uploads_playlist_id = (
                ((channel_data.get("contentDetails") or {}).get("relatedPlaylists") or {})
                .get("uploads")
            )
            if isinstance(uploads_playlist_id, str) and uploads_playlist_id:
                playlist_id_set.add(uploads_playlist_id)
                playlist_ids.append(uploads_playlist_id)

            for section in sections_items:
                if not isinstance(section, dict):
                    continue
                content_details = section.get("contentDetails") or {}
                playlists = content_details.get("playlists") or []
                for playlist_id in playlists:
                    if (
                        isinstance(playlist_id, str)
                        and playlist_id
                        and playlist_id not in playlist_id_set
                    ):
                        playlist_id_set.add(playlist_id)
                        playlist_ids.append(playlist_id)
            playlist_metadata_map: dict[str, dict[str, Any]] = {}
            if playlist_ids:
                playlist_metadata_items = await fetch_youtube_playlists(
                    playlist_ids, api_key
                )
                playlist_metadata_map = {
                    item.get("id"): item
                    for item in playlist_metadata_items
                    if isinstance(item, dict) and item.get("id")
                }
            playlist_items_map: dict[str, list[dict[str, Any]]] = {}
            for playlist_id in playlist_ids:
                _, playlist_items_response = await fetch_youtube_section_data(
                    "playlists", playlist_id, api_key
                )
                playlist_items_map[playlist_id] = (
                    playlist_items_response.get("items") or []
                )
            retrieved_at = datetime.now(timezone.utc)

            def _persist_channel() -> None:
                save_channel(channel_data, retrieved_at=retrieved_at)
                save_channel_sections(
                    channel_id,
                    sections_items,
                    retrieved_at=retrieved_at,
                )
                for playlist_id in playlist_ids:
                    items_to_save = playlist_items_map.get(playlist_id) or []
                    save_playlist_items(playlist_id, items_to_save)
                    playlist_metadata = playlist_metadata_map.get(playlist_id)
                    if playlist_metadata:
                        save_playlist(playlist_metadata, retrieved_at=retrieved_at)
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
            form_action=app.url_path_for(CREATE_ROUTE_NAMES[normalized_section]),
            resource_value=stripped_resource_id,
        )

    @app.get(
        "/configure/channels",
        response_class=HTMLResponse,
        name="configure_channels",
    )
    async def configure_channels(request: Request) -> HTMLResponse:
        channels = await run_in_threadpool(fetch_all_channels)
        content = _channels_overview_content(channels)
        heading = f"Manage {CONFIG_LABELS['channels']}"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="channels",
            content=content,
            form_action=app.url_path_for(CREATE_ROUTE_NAMES["channels"]),
        )

    @app.get(
        "/configure/playlists",
        response_class=HTMLResponse,
        name="configure_playlists",
    )
    async def configure_playlists(request: Request) -> HTMLResponse:
        playlists = await run_in_threadpool(fetch_all_playlists)
        content = _playlists_overview_content(playlists)
        heading = f"Manage {CONFIG_LABELS['playlists']}"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="playlists",
            content=content,
            form_action=app.url_path_for(CREATE_ROUTE_NAMES["playlists"]),
        )

    @app.get(
        "/configure/videos",
        response_class=HTMLResponse,
        name="configure_videos",
    )
    async def configure_videos(request: Request) -> HTMLResponse:
        videos = await run_in_threadpool(fetch_all_videos)
        content = _videos_overview_content(videos)
        heading = f"Manage {CONFIG_LABELS['videos']}"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="videos",
            content=content,
            form_action=app.url_path_for(CREATE_ROUTE_NAMES["videos"]),
        )

    @app.post("/configure/channels", name="create_channels")
    async def create_channels(
        request: Request,
        resource_id: str = Form(..., alias="resource_id"),
        list_choice: str = Form(..., alias="list"),
    ) -> Response:
        return await _create_resource(request, "channels", resource_id, list_choice)

    @app.post("/configure/playlists", name="create_playlists")
    async def create_playlists(
        request: Request,
        resource_id: str = Form(..., alias="resource_id"),
        list_choice: str = Form(..., alias="list"),
    ) -> Response:
        return await _create_resource(request, "playlists", resource_id, list_choice)

    @app.post("/configure/videos", name="create_videos")
    async def create_videos(
        request: Request,
        resource_id: str = Form(..., alias="resource_id"),
        list_choice: str = Form(..., alias="list"),
    ) -> Response:
        return await _create_resource(request, "videos", resource_id, list_choice)

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
            playlist = await run_in_threadpool(fetch_playlist, resource_id)
            content = _playlist_resource_content(
                resource_id, playlist_items, list, playlist
            )
        elif normalized_section == "channels":
            channel = await run_in_threadpool(fetch_channel, resource_id)
            channel_sections = await run_in_threadpool(
                fetch_channel_sections, resource_id
            )
            content = _channel_resource_content(channel, channel_sections)
        elif normalized_section == "videos":
            video = await run_in_threadpool(fetch_video, resource_id)
            content = _video_resource_content(video)
        else:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail="Unsupported configuration section")
        heading = f"{CONFIG_LABELS[normalized_section]} Resource"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section=normalized_section,
            content=content,
            form_action=app.url_path_for(CREATE_ROUTE_NAMES[normalized_section]),
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

