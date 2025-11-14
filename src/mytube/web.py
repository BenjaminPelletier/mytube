"""FastAPI application that exposes a Chromecast remote."""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
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
from .lounge import LoungeManager, coerce_auth_state
from .db import (
    fetch_all_channels,
    fetch_all_playlists,
    fetch_all_videos,
    fetch_channel,
    fetch_channel_sections,
    fetch_listed_video,
    fetch_listed_videos,
    fetch_playlist,
    fetch_playlist_items,
    fetch_video,
    initialize_database,
    repopulate_listed_videos,
    fetch_settings,
    save_channel,
    save_channel_sections,
    save_playlist,
    save_playlist_items,
    save_video,
    store_settings,
    set_resource_label,
)
from .ytlounge import PairingError, dumps_auth_payload
from .youtube import (
    fetch_youtube_channel_sections,
    fetch_youtube_channels,
    fetch_youtube_playlist_items,
    fetch_youtube_playlists,
    fetch_youtube_videos,
    load_youtube_api_key,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
static_directory = BASE_DIR / "static"

YOUTUBE_VIDEO_ID = "CYlon2tvywA"
LOUNGE_REMOTE_NAME = "MyTube Remote"

RESOURCE_NAVIGATION = (
    ("channels", "Channels"),
    ("playlists", "Playlists"),
    ("videos", "Videos"),
)
LIST_NAVIGATION = (("whitelist", "Whitelist"), ("blacklist", "Blacklist"))
SETTINGS_NAVIGATION = (("settings", "Settings"),)
CONFIG_NAVIGATION = RESOURCE_NAVIGATION + LIST_NAVIGATION + SETTINGS_NAVIGATION
RESOURCE_LABELS = {slug: label for slug, label in RESOURCE_NAVIGATION}
LIST_PAGE_LABELS = {slug: label for slug, label in LIST_NAVIGATION}
LIST_PAGE_FIELDS = {"whitelist": "whitelisted_by", "blacklist": "blacklisted_by"}
LISTED_VIDEO_FIELD_PREFIXES = {
    "whitelisted_by": "👍",
    "blacklisted_by": "👎",
}

LIST_LABELS = {"white": "Whitelist", "black": "Blacklist"}
LIST_TO_RESOURCE_LABEL = {"white": "whitelisted", "black": "blacklisted"}

CONFIG_ROUTE_NAMES = {
    "channels": "configure_channels",
    "playlists": "configure_playlists",
    "videos": "configure_videos",
    "whitelist": "configure_whitelist",
    "blacklist": "configure_blacklist",
    "settings": "configure_settings",
}

CREATE_ROUTE_NAMES = {
    "channels": "create_channel",
    "playlists": "create_playlist",
    "videos": "create_video",
}


def _validate_section(section: str) -> str:
    normalized = section.lower()
    if normalized not in RESOURCE_LABELS:
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
    template_name: str,
    context: dict[str, Any] | None = None,
    form_action: str | None = None,
    resource_value: str = "",
    show_resource_form: bool = True,
    navigation: list[dict[str, str | bool]] | None = None,
) -> HTMLResponse:
    navigation = navigation or _navigation_links(app, active_section)
    action = form_action
    if show_resource_form:
        if not action:
            form_section = (
                active_section
                if active_section in CREATE_ROUTE_NAMES
                else next(iter(CREATE_ROUTE_NAMES))
            )
            action = app.url_path_for(CREATE_ROUTE_NAMES[form_section])
    else:
        action = action or ""
    template_context: dict[str, Any] = {
        "request": request,
        "heading": heading,
        "navigation": navigation,
        "form_action": action,
        "resource_value": resource_value,
        "show_resource_form": show_resource_form,
    }
    if context:
        template_context.update(context)
    return templates.TemplateResponse(template_name, template_context)


def _resource_vote(label: str | None) -> str:
    if label == "whitelisted":
        return "👍"
    if label == "blacklisted":
        return "👎"
    return ""


def _channels_overview_content(channels: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for channel in channels:
        channel_id = channel.get("id") or ""
        if not channel_id:
            continue
        title = channel.get("title") or channel_id
        vote = _resource_vote(channel.get("label"))
        encoded_id = quote(channel_id, safe="")
        items.append(
            {
                "title": title,
                "url": f"/configure/channels/{encoded_id}",
                "vote": vote,
            }
        )
    return items


def _videos_overview_content(videos: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for video in videos:
        video_id = video.get("id") or ""
        if not video_id:
            continue
        title = video.get("title") or video_id
        vote = _resource_vote(video.get("label"))
        encoded_id = quote(video_id, safe="")
        items.append(
            {
                "title": title,
                "url": f"/configure/videos/{encoded_id}",
                "vote": vote,
            }
        )
    return items


def _listed_videos_content(
    list_slug: str, videos: list[dict[str, Any]], regenerate_url: str
) -> dict[str, Any]:
    if list_slug not in LIST_PAGE_FIELDS:
        raise HTTPException(status_code=404, detail="Unknown list page")

    heading_label = LIST_PAGE_LABELS.get(list_slug, list_slug.title())

    identifier_pool: set[str] = set()
    for video in videos:
        for field in ("whitelisted_by", "blacklisted_by"):
            values = video.get(field)
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str):
                    normalized = value.strip()
                    if normalized:
                        identifier_pool.add(normalized)

    reference_map = (
        _build_resource_reference_map(sorted(identifier_pool))
        if identifier_pool
        else {}
    )

    items: list[dict[str, Any]] = []
    for video in videos:
        video_id = video.get("video_id") or ""
        if not video_id:
            continue
        title = video.get("title") or video_id
        encoded_video_id = quote(video_id, safe="")
        items.append(
            {
                "title": title,
                "url": f"/configure/videos/{encoded_video_id}",
                "listed_groups": _build_listed_groups(video, reference_map),
            }
        )

    return {
        "heading_label": heading_label,
        "regenerate_url": regenerate_url,
        "videos": items,
    }


def _settings_content(
    devices_url: str,
    settings: dict[str, str],
    save_url: str,
    pair_url: str,
    lounge_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "devices_url": devices_url,
        "save_url": save_url,
        "pair_url": pair_url,
        "settings": settings or {},
        "lounge_status": lounge_status,
    }


def _load_lounge_auth(settings: Mapping[str, Any]) -> dict[str, Any] | None:
    """Deserialize stored lounge credentials, if present."""

    raw_value = settings.get("youtube_app_auth")
    if raw_value is None:
        return None

    if isinstance(raw_value, str) and not raw_value.strip():
        return None

    try:
        return coerce_auth_state(raw_value)
    except ValueError:
        logger.warning("Stored YouTube app auth payload is invalid.")
    return None


def _playlists_overview_content(playlists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for playlist in playlists:
        playlist_id = playlist.get("id") or ""
        if not playlist_id:
            continue

        title = playlist.get("title") or playlist_id
        vote = _resource_vote(playlist.get("label"))
        encoded_playlist_id = quote(playlist_id, safe="")

        channel_id = playlist.get("channel_id") or ""
        channel_title = (
            playlist.get("channel_title")
            or channel_id
            or "Unknown channel"
        )
        channel_url = f"/configure/channels/{quote(channel_id, safe='')}" if channel_id else None

        items.append(
            {
                "title": title,
                "url": f"/configure/playlists/{encoded_playlist_id}",
                "vote": vote,
                "channel": {
                    "title": channel_title,
                    "url": channel_url,
                },
            }
        )

    return items


def _channel_resource_content(
    channel: dict | None,
    sections: list[dict],
    playlist_map: dict[str, dict] | None = None,
) -> dict[str, Any]:
    if not channel:
        return {"channel": None}

    channel_identifier = channel.get("id") or ""
    encoded_channel_id = quote(channel_identifier, safe="") if channel_identifier else ""
    playlist_map = playlist_map or {}

    section_entries: list[dict[str, str | None]] = []
    for section in sections or []:
        raw = section.get("raw_json") if isinstance(section, dict) else None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:  # pragma: no cover - defensive
                raw = None
        if not raw and isinstance(section, dict):
            raw = section
        raw = raw if isinstance(raw, dict) else {}

        content_details = raw.get("contentDetails")
        if not isinstance(content_details, dict):
            continue

        playlists_value = content_details.get("playlists")
        playlist_ids: list[str] = []
        if isinstance(playlists_value, list):
            playlist_ids = [
                playlist_id
                for playlist_id in playlists_value
                if isinstance(playlist_id, str) and playlist_id
            ]
        elif isinstance(playlists_value, str) and playlists_value:
            playlist_ids = [playlists_value]

        if len(playlist_ids) == 1:
            playlist_id = playlist_ids[0]
            playlist_record = playlist_map.get(playlist_id) or {}
            playlist_title = (
                playlist_record.get("title") if isinstance(playlist_record, dict) else None
            )
            display_text = playlist_title or playlist_id
            section_entries.append(
                {
                    "title": display_text,
                    "url": f"/configure/playlists/{quote(playlist_id, safe='')}",
                }
            )
        else:
            title_text = section.get("title") if isinstance(section, dict) else None
            if not title_text:
                snippet = raw.get("snippet") if isinstance(raw, dict) else {}
                if isinstance(snippet, dict):
                    title_text = snippet.get("title")
            if not title_text and isinstance(section, dict):
                title_text = section.get("id")
            section_entries.append({"title": title_text or "Untitled", "url": None})

    return {
        "channel": {
            "id": channel_identifier,
            "title": channel.get("title") or "Untitled channel",
            "description": channel.get("description") or "",
            "retrieved_at": channel.get("retrieved_at") or "Unknown",
            "label": channel.get("label"),
            "vote": _resource_vote(channel.get("label")),
            "info_url": (
                f"/configure/channels/{encoded_channel_id}/raw" if encoded_channel_id else None
            ),
            "sections": section_entries,
        }
    }


def _playlist_resource_content(
    playlist_id: str,
    playlist_items: list[dict[str, Any]],
    list_choice: str | None,
    playlist: dict | None,
) -> dict[str, Any]:
    playlist_id_str = str(playlist_id)
    encoded_playlist_id = quote(playlist_id_str, safe="")
    list_label = LIST_LABELS.get(list_choice)

    playlist_context: dict[str, Any] = {
        "id": playlist_id_str,
        "list_label": list_label,
        "items": [],
        "info_url": (
            f"/configure/playlists/{encoded_playlist_id}/raw" if playlist else None
        ),
    }

    if playlist:
        playlist_context["title"] = playlist.get("title") or playlist_id_str
        raw_data = playlist.get("raw_json") if isinstance(playlist, dict) else None
        if isinstance(raw_data, str):
            try:
                raw_data = json.loads(raw_data)
            except json.JSONDecodeError:  # pragma: no cover - defensive
                raw_data = None
        if not raw_data and isinstance(playlist, dict):
            raw_data = playlist
        snippet = raw_data.get("snippet") if isinstance(raw_data, dict) else {}
        channel_id = snippet.get("channelId") if isinstance(snippet, dict) else None
        channel_title = snippet.get("channelTitle") if isinstance(snippet, dict) else None
        if channel_id or channel_title:
            playlist_context["channel"] = {
                "title": channel_title or channel_id or "",
                "url": (
                    f"/configure/channels/{quote(channel_id, safe='')}"
                    if channel_id
                    else None
                ),
            }
    else:
        playlist_context["title"] = None

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

        if video_id:
            encoded_video_id = quote(str(video_id), safe="")
            playlist_context["items"].append(
                {
                    "title": title,
                    "video_id": str(video_id),
                    "video_url": f"/configure/videos/{encoded_video_id}",
                }
            )
        else:
            playlist_context["items"].append(
                {
                    "title": title,
                    "video_id": None,
                    "video_url": None,
                }
            )

    return {"playlist": playlist_context}


def _build_resource_reference_map(
    identifiers: Iterable[str],
) -> dict[str, dict[str, str]]:
    """Resolve identifiers to stored resource metadata."""

    results: dict[str, dict[str, str]] = {}
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        normalized = identifier.strip()
        if not normalized or normalized in results:
            continue

        channel = fetch_channel(normalized)
        if channel:
            title = channel.get("title") or normalized
            encoded = quote(normalized, safe="")
            results[normalized] = {
                "title": title,
                "url": f"/configure/channels/{encoded}",
            }
            continue

        playlist = fetch_playlist(normalized)
        if playlist:
            title = playlist.get("title") or normalized
            encoded = quote(normalized, safe="")
            results[normalized] = {
                "title": title,
                "url": f"/configure/playlists/{encoded}",
            }
            continue

        video = fetch_video(normalized)
        if video:
            title = video.get("title") or normalized
            encoded = quote(normalized, safe="")
            results[normalized] = {
                "title": title,
                "url": f"/configure/videos/{encoded}",
            }
            continue

        encoded = quote(normalized, safe="")
        results[normalized] = {
            "title": normalized,
            "url": f"/configure/videos/{encoded}",
        }

    return results


def _build_list_entry(identifier: str, reference_map: dict[str, dict[str, str]]) -> dict[str, str]:
    info = reference_map.get(identifier)
    if info:
        title = info.get("title") or identifier
        url = info.get("url") or f"/configure/videos/{quote(identifier, safe='')}"
    else:
        title = identifier
        url = f"/configure/videos/{quote(identifier, safe='')}"
    return {"title": title, "url": url}


def _build_listed_groups(
    listed_video: dict[str, Any] | None,
    reference_map: dict[str, dict[str, str]] | None,
) -> list[dict[str, Any]]:
    if not listed_video:
        return []

    effective_map = reference_map or {}
    groups: list[dict[str, Any]] = []
    for key, icon in (("whitelisted_by", "👍"), ("blacklisted_by", "👎")):
        identifiers = listed_video.get(key)
        if not isinstance(identifiers, list):
            continue
        entries: list[dict[str, str]] = []
        for identifier in identifiers:
            if not isinstance(identifier, str):
                continue
            normalized = identifier.strip()
            if not normalized:
                continue
            entries.append(_build_list_entry(normalized, effective_map))
        if entries:
            groups.append({"icon": icon, "entries": entries})
    return groups


def _video_resource_content(
    video: dict | None,
    listed_video: dict[str, Any] | None = None,
    reference_map: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    if not video:
        return {"video": None}

    video_identifier = video.get("id") or ""
    encoded_video_id = quote(video_identifier, safe="") if video_identifier else ""

    return {
        "video": {
            "id": video_identifier,
            "title": video.get("title") or "Untitled video",
            "description": video.get("description") or "",
            "retrieved_at": video.get("retrieved_at") or "Unknown",
            "label": video.get("label"),
            "vote": _resource_vote(video.get("label")),
            "info_url": (
                f"/configure/videos/{encoded_video_id}/raw" if encoded_video_id else None
            ),
            "listed_groups": _build_listed_groups(listed_video, reference_map),
        }
    }


def _raw_payload_content(resource_label: str, resource_id: str, payload: Any) -> dict[str, Any]:
    return {
        "resource_label": resource_label,
        "resource_id": resource_id,
        "payload_json": json.dumps(payload, indent=2, sort_keys=True),
    }


def _select_thumbnail_url(raw_video: Any, desired_width: int = 320) -> str | None:
    if not isinstance(raw_video, dict):
        return None

    snippet = raw_video.get("snippet")
    if not isinstance(snippet, dict):
        return None

    thumbnails = snippet.get("thumbnails")
    if not isinstance(thumbnails, dict):
        return None

    best_url: str | None = None
    best_diff = float("inf")
    best_width: int | None = None
    fallback_url: str | None = None

    for data in thumbnails.values():
        if not isinstance(data, dict):
            continue
        url = data.get("url")
        if not isinstance(url, str) or not url:
            continue
        if fallback_url is None:
            fallback_url = url
        width_value = data.get("width")
        width = width_value if isinstance(width_value, int) else None
        if width is None:
            continue
        diff = abs(width - desired_width)
        if diff < best_diff:
            best_diff = diff
            best_url = url
            best_width = width
        elif diff == best_diff and best_width is not None and width > best_width:
            best_url = url
            best_width = width

    if best_url:
        return best_url
    return fallback_url


async def _load_video_record(video_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch a video record, refreshing it from YouTube if needed."""

    video_record = await run_in_threadpool(fetch_video, video_id)
    if video_record and video_record.get("raw_json"):
        return video_record, None

    api_key = load_youtube_api_key()
    _, payload = await fetch_youtube_videos(video_id, api_key)
    items = payload.get("items") or []
    if not items:
        return None, "No video information available for that ID."

    video = items[0]
    await run_in_threadpool(
        save_video,
        video,
        retrieved_at=datetime.now(timezone.utc),
    )
    refreshed_record = await run_in_threadpool(fetch_video, video_id)
    if refreshed_record and refreshed_record.get("raw_json"):
        return refreshed_record, None
    return refreshed_record, "Unable to load video details."


def _build_playing_context(video_record: dict[str, Any], video_id: str) -> dict[str, str | None]:
    title = video_record.get("title") or video_id
    raw_payload = video_record.get("raw_json")
    thumbnail_url = _select_thumbnail_url(raw_payload)
    return {
        "title": title,
        "thumbnail_url": thumbnail_url,
        "video_id": video_id,
    }


def create_app() -> FastAPI:
    """Create a configured FastAPI application instance."""

    initialize_database()
    app = FastAPI(title="MyTube Remote")

    lounge_manager = LoungeManager(default_name=LOUNGE_REMOTE_NAME)
    app.state.lounge = lounge_manager

    @app.on_event("startup")
    async def startup_lounge_manager() -> None:
        settings = await run_in_threadpool(fetch_settings, ["youtube_app_auth"])
        auth_state = _load_lounge_auth(settings)
        if not auth_state:
            return
        try:
            await lounge_manager.upsert_from_auth(auth_state, name=LOUNGE_REMOTE_NAME)
        except (PairingError, RuntimeError, ValueError) as exc:  # pragma: no cover - best effort startup
            logger.warning(
                "Unable to connect to the paired YouTube TV during startup: %s",
                exc,
                exc_info=True,
            )

    @app.on_event("shutdown")
    async def shutdown_lounge_manager() -> None:
        await lounge_manager.shutdown()

    if static_directory.exists():
        app.mount("/static", StaticFiles(directory=str(static_directory)), name="static")

    def _render(
        request: Request,
        status: str | None = None,
        playing: dict[str, str | None] | None = None,
        videos: list[dict[str, str | None]] | None = None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            "home.html",
            {
                "request": request,
                "cast_url": app.url_path_for("cast_featured"),
                "devices_url": app.url_path_for("list_devices"),
                "videos_api_url": app.url_path_for("list_random_videos"),
                "status": status,
                "playing": playing,
                "videos": videos or [],
            },
        )

    @app.get("/", response_class=HTMLResponse)
    async def home(
        request: Request, playing: str | None = Query(default=None, min_length=1)
    ) -> HTMLResponse:
        playing_video: dict[str, str | None] | None = None
        status_message: str | None = None

        if playing:
            video_id = playing.strip()
            if video_id:
                video_record, load_error = await _load_video_record(video_id)

                if video_record and video_record.get("raw_json"):
                    playing_video = _build_playing_context(video_record, video_id)
                elif load_error:
                    status_message = load_error
                elif not status_message:
                    status_message = "Unable to load video details."

        return _render(
            request,
            status=status_message,
            playing=playing_video,
            videos=[],
        )

    async def _random_video_options(limit: int = 5) -> list[dict[str, str | None]]:
        if limit <= 0:
            return []

        listed_videos = await run_in_threadpool(fetch_listed_videos, "whitelist")
        approved_videos = [
            video
            for video in listed_videos
            if video.get("video_id") and not video.get("blacklisted_by")
        ]
        if not approved_videos:
            return []

        random.shuffle(approved_videos)

        video_options: list[dict[str, str | None]] = []
        for entry in approved_videos:
            if len(video_options) >= limit:
                break
            video_id = entry.get("video_id")
            if not video_id:
                continue

            video_record, _ = await _load_video_record(video_id)
            if not video_record or not video_record.get("raw_json"):
                continue

            thumbnail_url = _select_thumbnail_url(video_record.get("raw_json"))
            if not thumbnail_url:
                continue

            title = video_record.get("title") or entry.get("title") or video_id
            video_options.append(
                {
                    "video_id": video_id,
                    "title": title,
                    "thumbnail_url": thumbnail_url,
                    "cast_url": app.url_path_for("cast_video", video_id=video_id),
                }
            )

        return video_options

    @app.get("/videos/random", name="list_random_videos")
    async def list_random_videos(limit: int = Query(default=5, ge=1, le=20)) -> dict[str, Any]:
        """Return a random selection of approved videos."""

        videos = await _random_video_options(limit=limit)
        return {"videos": videos}

    @app.get("/configure", response_class=HTMLResponse)
    async def configure_home(request: Request) -> HTMLResponse:
        return _render_config_page(
            request,
            app,
            heading="Configure MyTube",
            active_section=None,
            template_name="configure/index.html",
            form_action=app.url_path_for(CREATE_ROUTE_NAMES["channels"]),
        )

    @app.get(
        "/configure/channels",
        response_class=HTMLResponse,
        name="configure_channels",
    )
    async def configure_channels(request: Request) -> HTMLResponse:
        channels = await run_in_threadpool(fetch_all_channels)
        channel_items = _channels_overview_content(channels)
        heading = f"Manage {RESOURCE_LABELS['channels']}"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="channels",
            template_name="configure/channels_overview.html",
            context={"channels": channel_items},
            form_action=app.url_path_for(CREATE_ROUTE_NAMES["channels"]),
        )

    @app.get(
        "/configure/playlists",
        response_class=HTMLResponse,
        name="configure_playlists",
    )
    async def configure_playlists(request: Request) -> HTMLResponse:
        playlists = await run_in_threadpool(fetch_all_playlists)
        playlist_items = _playlists_overview_content(playlists)
        heading = f"Manage {RESOURCE_LABELS['playlists']}"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="playlists",
            template_name="configure/playlists_overview.html",
            context={"playlists": playlist_items},
            form_action=app.url_path_for(CREATE_ROUTE_NAMES["playlists"]),
        )

    @app.get(
        "/configure/videos",
        response_class=HTMLResponse,
        name="configure_videos",
    )
    async def configure_videos(request: Request) -> HTMLResponse:
        videos = await run_in_threadpool(fetch_all_videos)
        video_items = _videos_overview_content(videos)
        heading = f"Manage {RESOURCE_LABELS['videos']}"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="videos",
            template_name="configure/videos_overview.html",
            context={"videos": video_items},
            form_action=app.url_path_for(CREATE_ROUTE_NAMES["videos"]),
        )

    @app.get(
        "/configure/whitelist",
        response_class=HTMLResponse,
        name="configure_whitelist",
    )
    async def configure_whitelist(request: Request) -> HTMLResponse:
        videos = await run_in_threadpool(fetch_listed_videos, "whitelist")
        regenerate_url = app.url_path_for(
            "regenerate_listed_videos", list_slug="whitelist"
        )
        list_context = _listed_videos_content("whitelist", videos, regenerate_url)
        heading = f"{LIST_PAGE_LABELS['whitelist']} Videos"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="whitelist",
            template_name="configure/listed_videos.html",
            context=list_context,
            show_resource_form=False,
        )

    @app.get(
        "/configure/blacklist",
        response_class=HTMLResponse,
        name="configure_blacklist",
    )
    async def configure_blacklist(request: Request) -> HTMLResponse:
        videos = await run_in_threadpool(fetch_listed_videos, "blacklist")
        regenerate_url = app.url_path_for(
            "regenerate_listed_videos", list_slug="blacklist"
        )
        list_context = _listed_videos_content("blacklist", videos, regenerate_url)
        heading = f"{LIST_PAGE_LABELS['blacklist']} Videos"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="blacklist",
            template_name="configure/listed_videos.html",
            context=list_context,
            show_resource_form=False,
        )

    @app.get(
        "/configure/settings",
        response_class=HTMLResponse,
        name="configure_settings",
    )
    async def configure_settings(request: Request) -> HTMLResponse:
        devices_url = app.url_path_for("list_devices")
        settings = await run_in_threadpool(
            fetch_settings, ["preferred_device", "youtube_app_auth"]
        )
        save_url = app.url_path_for("save_settings")
        pair_url = app.url_path_for("pair_youtube_app")
        lounge_status: dict[str, Any] | None = None
        auth_state = _load_lounge_auth(settings)
        lounge_manager_dep = getattr(app.state, "lounge", None)
        if isinstance(lounge_manager_dep, LoungeManager) and auth_state:
            screen_id = auth_state.get("screenId")
            try:
                controller = await lounge_manager_dep.get(screen_id) if screen_id else None
                if controller is None and screen_id:
                    controller = await lounge_manager_dep.upsert_from_auth(
                        auth_state, name=LOUNGE_REMOTE_NAME
                    )
                if controller is not None:
                    lounge_status = await controller.get_status()
            except (PairingError, RuntimeError, ValueError) as exc:  # pragma: no cover - best effort status
                logger.warning(
                    "Unable to retrieve YouTube TV status for screen %s: %s",
                    screen_id,
                    exc,
                    exc_info=True,
                )
                lounge_status = {
                    "screen_id": screen_id,
                    "name": LOUNGE_REMOTE_NAME,
                    "connected": False,
                    "error": str(exc),
                }

        settings_context = _settings_content(
            devices_url, settings, save_url, pair_url, lounge_status
        )
        return _render_config_page(
            request,
            app,
            heading="Application Settings",
            active_section="settings",
            template_name="configure/settings.html",
            context=settings_context,
            show_resource_form=False,
        )

    @app.post(
        "/configure/settings",
        name="save_settings",
    )
    async def save_settings_handler(request: Request) -> Response:
        form_data = await request.form()
        settings_payload: dict[str, str | None] = {}
        for key, value in form_data.multi_items():
            normalized_key = str(key)
            if hasattr(value, "filename") and hasattr(value, "file"):
                continue
            if isinstance(value, str) or value is None:
                settings_payload[normalized_key] = value
            else:
                settings_payload[normalized_key] = str(value)

        await run_in_threadpool(store_settings, settings_payload)
        redirect_url = app.url_path_for("configure_settings")
        return RedirectResponse(url=redirect_url, status_code=303)

    @app.post(
        "/configure/settings/pair",
        name="pair_youtube_app",
    )
    async def pair_youtube_app_handler(request: Request) -> dict[str, str]:
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc

        code_value: str | None = None
        if isinstance(payload, dict):
            raw_code = payload.get("code")
            if isinstance(raw_code, str):
                code_value = raw_code.strip()

        if not code_value:
            raise HTTPException(
                status_code=400,
                detail="Link with TV code is required.",
            )

        lounge_manager_dep = getattr(request.app.state, "lounge", None)
        if not isinstance(lounge_manager_dep, LoungeManager):
            raise HTTPException(
                status_code=503,
                detail="TV lounge controller is not available.",
            )

        try:
            payload = await lounge_manager_dep.pair_with_code(
                code_value, name=LOUNGE_REMOTE_NAME
            )
        except PairingError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except (RuntimeError, ValueError) as exc:  # pragma: no cover - defensive
            logger.exception("Unexpected error while pairing with YouTube app: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="Unable to pair with the YouTube app.",
            ) from exc

        json_payload = dumps_auth_payload(payload)
        await run_in_threadpool(store_settings, {"youtube_app_auth": json_payload})
        return {"status": "ok"}

    @app.post("/configure/channels", name="create_channel")
    async def create_channel(
        resource_id: str = Form(..., alias="resource_id"),
        list_choice: str = Form(..., alias="list"),
    ) -> Response:
        stripped_resource_id = resource_id.strip()
        if not stripped_resource_id:
            raise HTTPException(status_code=400, detail="Resource ID is required")
        if list_choice not in LIST_LABELS:
            raise HTTPException(status_code=400, detail="Unknown list selection")

        api_key = load_youtube_api_key()
        _, response_data = await fetch_youtube_channels(
            stripped_resource_id, api_key
        )
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

        for section_data in sections_items:
            if not isinstance(section_data, dict):
                continue
            content_details = section_data.get("contentDetails") or {}
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
            playlist_metadata_items = await fetch_youtube_playlists(playlist_ids, api_key)
            playlist_metadata_map = {
                item.get("id"): item
                for item in playlist_metadata_items
                if isinstance(item, dict) and item.get("id")
            }

        playlist_items_map: dict[str, list[dict[str, Any]]] = {}
        for playlist_id in playlist_ids:
            _, playlist_items_response = await fetch_youtube_playlist_items(
                playlist_id, api_key
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
                save_playlist_items(
                    playlist_id,
                    items_to_save,
                    retrieved_at=retrieved_at,
                )
                playlist_metadata = playlist_metadata_map.get(playlist_id)
                if playlist_metadata:
                    save_playlist(playlist_metadata, retrieved_at=retrieved_at)
            set_resource_label("channel", channel_id, label)

        await run_in_threadpool(_persist_channel)

        redirect_url = app.url_path_for(
            "view_resource",
            section="channels",
            resource_id=channel_id,
        )
        return RedirectResponse(redirect_url, status_code=303)

    @app.post("/configure/playlists", name="create_playlist")
    async def create_playlist(
        resource_id: str = Form(..., alias="resource_id"),
        list_choice: str = Form(..., alias="list"),
    ) -> Response:
        stripped_resource_id = resource_id.strip()
        if not stripped_resource_id:
            raise HTTPException(status_code=400, detail="Resource ID is required")
        if list_choice not in LIST_LABELS:
            raise HTTPException(status_code=400, detail="Unknown list selection")

        api_key = load_youtube_api_key()
        _, response_data = await fetch_youtube_playlist_items(
            stripped_resource_id, api_key
        )

        playlist_items = response_data.get("items", [])
        retrieved_at = datetime.now(timezone.utc)
        await run_in_threadpool(
            save_playlist_items,
            stripped_resource_id,
            playlist_items,
            retrieved_at=retrieved_at,
        )

        playlist_metadata_items = await fetch_youtube_playlists(
            [stripped_resource_id], api_key
        )
        playlist_data = playlist_metadata_items[0] if playlist_metadata_items else None
        label = LIST_TO_RESOURCE_LABEL.get(list_choice)
        if label is None:
            raise HTTPException(status_code=400, detail="Unknown list selection")

        def _persist_playlist() -> None:
            if playlist_data:
                save_playlist(playlist_data, retrieved_at=retrieved_at)
            set_resource_label("playlist", stripped_resource_id, label)

        await run_in_threadpool(_persist_playlist)

        redirect_url = app.url_path_for(
            "view_resource",
            section="playlists",
            resource_id=stripped_resource_id,
        )
        redirect_url = f"{redirect_url}?list={list_choice}"
        return RedirectResponse(redirect_url, status_code=303)

    @app.post("/configure/videos", name="create_video")
    async def create_video(
        resource_id: str = Form(..., alias="resource_id"),
        list_choice: str = Form(..., alias="list"),
    ) -> Response:
        stripped_resource_id = resource_id.strip()
        if not stripped_resource_id:
            raise HTTPException(status_code=400, detail="Resource ID is required")
        if list_choice not in LIST_LABELS:
            raise HTTPException(status_code=400, detail="Unknown list selection")

        api_key = load_youtube_api_key()
        _, response_data = await fetch_youtube_videos(
            stripped_resource_id, api_key
        )
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
            section="videos",
            resource_id=video_id,
        )
        return RedirectResponse(redirect_url, status_code=303)

    @app.post(
        "/configure/{list_slug}/regenerate",
        name="regenerate_listed_videos",
    )
    async def regenerate_listed_videos_endpoint(list_slug: str) -> Response:
        normalized = list_slug.lower()
        if normalized not in LIST_PAGE_FIELDS:
            raise HTTPException(status_code=404, detail="Unknown list page")

        await run_in_threadpool(repopulate_listed_videos)

        redirect_route = CONFIG_ROUTE_NAMES.get(normalized)
        if not redirect_route:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail="Unable to determine redirect route")
        redirect_url = app.url_path_for(redirect_route)
        return RedirectResponse(redirect_url, status_code=303)

    @app.get(
        "/configure/channels/{resource_id}/raw",
        response_class=HTMLResponse,
        name="view_channel_raw",
    )
    async def view_channel_raw(request: Request, resource_id: str) -> HTMLResponse:
        channel = await run_in_threadpool(fetch_channel, resource_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        raw_payload = channel.get("raw_json")
        if raw_payload is None:
            raise HTTPException(
                status_code=404, detail="Channel raw data is unavailable"
            )
        payload_context = _raw_payload_content("Channel", resource_id, raw_payload)
        return _render_config_page(
            request,
            app,
            heading="Channel raw data",
            active_section="channels",
            template_name="configure/raw_payload.html",
            context=payload_context,
            show_resource_form=False,
        )

    @app.get(
        "/configure/playlists/{resource_id}/raw",
        response_class=HTMLResponse,
        name="view_playlist_raw",
    )
    async def view_playlist_raw(request: Request, resource_id: str) -> HTMLResponse:
        playlist = await run_in_threadpool(fetch_playlist, resource_id)
        if not playlist:
            raise HTTPException(status_code=404, detail="Playlist not found")
        raw_payload = playlist.get("raw_json")
        if raw_payload is None:
            raise HTTPException(
                status_code=404, detail="Playlist raw data is unavailable"
            )
        payload_context = _raw_payload_content("Playlist", resource_id, raw_payload)
        return _render_config_page(
            request,
            app,
            heading="Playlist raw data",
            active_section="playlists",
            template_name="configure/raw_payload.html",
            context=payload_context,
            show_resource_form=False,
        )

    @app.get(
        "/configure/videos/{resource_id}/raw",
        response_class=HTMLResponse,
        name="view_video_raw",
    )
    async def view_video_raw(request: Request, resource_id: str) -> HTMLResponse:
        video = await run_in_threadpool(fetch_video, resource_id)
        if not video:
            raise HTTPException(status_code=404, detail="Video not found")
        raw_payload = video.get("raw_json")
        if raw_payload is None:
            raise HTTPException(
                status_code=404, detail="Video raw data is unavailable"
            )
        payload_context = _raw_payload_content("Video", resource_id, raw_payload)
        return _render_config_page(
            request,
            app,
            heading="Video raw data",
            active_section="videos",
            template_name="configure/raw_payload.html",
            context=payload_context,
            show_resource_form=False,
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
        list_choice: str | None = Query(None, alias="list"),
    ) -> HTMLResponse:
        normalized_section = _validate_section(section)
        template_name: str
        resource_context: dict[str, Any]
        if normalized_section == "playlists":
            playlist_items = await run_in_threadpool(
                fetch_playlist_items, resource_id
            )
            playlist = await run_in_threadpool(fetch_playlist, resource_id)
            resource_context = _playlist_resource_content(
                resource_id, playlist_items, list_choice, playlist
            )
            template_name = "configure/playlist_resource.html"
        elif normalized_section == "channels":
            channel = await run_in_threadpool(fetch_channel, resource_id)
            channel_sections = await run_in_threadpool(
                fetch_channel_sections, resource_id
            )

            playlist_map: dict[str, dict] = {}
            if channel_sections:
                def _fetch_section_playlists() -> dict[str, dict]:
                    playlist_ids: set[str] = set()
                    for section in channel_sections:
                        raw = section.get("raw_json") if isinstance(section, dict) else None
                        if isinstance(raw, str):
                            try:
                                raw = json.loads(raw)
                            except json.JSONDecodeError:  # pragma: no cover - defensive
                                raw = None
                        if not raw and isinstance(section, dict):
                            raw = section
                        if not isinstance(raw, dict):
                            continue
                        content_details = raw.get("contentDetails")
                        if not isinstance(content_details, dict):
                            continue
                        playlists_value = content_details.get("playlists")
                        if isinstance(playlists_value, list):
                            for playlist_id in playlists_value:
                                if isinstance(playlist_id, str) and playlist_id:
                                    playlist_ids.add(playlist_id)
                        elif isinstance(playlists_value, str) and playlists_value:
                            playlist_ids.add(playlists_value)

                    playlist_map: dict[str, dict] = {}
                    for playlist_id in playlist_ids:
                        playlist = fetch_playlist(playlist_id)
                        if isinstance(playlist, dict):
                            playlist_map[playlist_id] = playlist
                    return playlist_map

                playlist_map = await run_in_threadpool(_fetch_section_playlists)
            resource_context = _channel_resource_content(
                channel, channel_sections, playlist_map
            )
            template_name = "configure/channel_resource.html"
        elif normalized_section == "videos":
            video = await run_in_threadpool(fetch_video, resource_id)
            listed_video = await run_in_threadpool(
                fetch_listed_video, resource_id
            )

            reference_map: dict[str, dict[str, str]] = {}
            if listed_video:
                identifiers: set[str] = set()
                for key in ("whitelisted_by", "blacklisted_by"):
                    entries = listed_video.get(key) or []
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if not isinstance(entry, str):
                            continue
                        normalized_entry = entry.strip()
                        if normalized_entry:
                            identifiers.add(normalized_entry)
                if identifiers:
                    reference_map = await run_in_threadpool(
                        _build_resource_reference_map, identifiers
                    )

            resource_context = _video_resource_content(
                video, listed_video, reference_map
            )
            template_name = "configure/video_resource.html"
        else:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail="Unsupported configuration section")
        heading = f"{RESOURCE_LABELS[normalized_section]} Resource"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section=normalized_section,
            template_name=template_name,
            context=resource_context,
            form_action=app.url_path_for(CREATE_ROUTE_NAMES[normalized_section]),
            resource_value=resource_id,
        )

    @app.get("/devices")
    async def list_devices() -> dict[str, list[str]]:
        devices = await run_in_threadpool(discover_chromecast_names)
        return {"devices": devices}

    @app.get("/cast/video/{video_id}", response_class=HTMLResponse)
    async def cast_video(
        request: Request, video_id: str, device: str | None = None
    ) -> Response:
        try:
            result: CastResult = await run_in_threadpool(
                cast_youtube_video, video_id, device_name=device
            )
        except ChromecastUnavailableError as exc:  # pragma: no cover - hardware required
            logger.warning("Chromecast unavailable: %s", exc)
            return _render(request, str(exc))

        status = (
            "Casting YouTube video "
            f"https://youtu.be/{result.video_id} to Chromecast '{result.device_name}'."
        )
        logger.info("%s", status)

        redirect_url = app.url_path_for("home")
        playing_param = quote(result.video_id, safe="")
        return RedirectResponse(
            url=f"{redirect_url}?playing={playing_param}", status_code=303
        )

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

