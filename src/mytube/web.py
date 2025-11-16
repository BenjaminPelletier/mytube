"""FastAPI application that exposes a Chromecast remote."""

from __future__ import annotations

import asyncio
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

from .lounge import LoungeManager, coerce_auth_state
from .db import (
    clear_resource_label,
    fetch_all_channels,
    fetch_all_playlists,
    fetch_all_videos,
    fetch_labeled_resources,
    VideoFilters,
    fetch_channel,
    fetch_channel_sections,
    fetch_history,
    fetch_history_event,
    fetch_listed_video,
    fetch_listed_videos,
    fetch_playlist,
    fetch_playlist_items,
    fetch_settings,
    fetch_resource_label,
    fetch_resource_labels_map,
    fetch_video,
    initialize_database,
    log_history_event,
    repopulate_listed_videos,
    refresh_listed_video_disqualifications,
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
    fetch_youtube_video_with_bonus,
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
HISTORY_NAVIGATION = (("history", "History"),)
SETTINGS_NAVIGATION = (("settings", "Settings"),)
CONFIG_NAVIGATION = (
    RESOURCE_NAVIGATION + LIST_NAVIGATION + HISTORY_NAVIGATION + SETTINGS_NAVIGATION
)
RESOURCE_LABELS = {slug: label for slug, label in RESOURCE_NAVIGATION}
LIST_PAGE_LABELS = {slug: label for slug, label in LIST_NAVIGATION}
VIDEO_FILTER_OPTIONS = (
    ("whitelisted", "Whitelisted"),
    ("blacklisted", "Blacklisted"),
    ("disqualified", "Disqualified"),
    ("favorites", "Favorites"),
    ("flagged", "Flagged"),
    ("has_details", "Has details"),
)
LIST_PAGE_FIELDS = {"whitelist": "whitelisted_by", "blacklist": "blacklisted_by"}
LISTED_VIDEO_FIELD_PREFIXES = {
    "whitelisted_by": "👍",
    "blacklisted_by": "👎",
}

LIST_LABELS = {"white": "Whitelist", "black": "Blacklist"}
LIST_TO_RESOURCE_LABEL = {"white": "whitelisted", "black": "blacklisted"}
RESOURCE_TYPE_ICONS = {
    "channel": "👤",
    "playlist": "≡",
    "video": "📹",
}

HISTORY_VIDEO_EVENT_TYPES = {
    "video.play",
    "video.favorite.toggle",
    "video.flag.toggle",
}

CONFIG_ROUTE_NAMES = {
    "channels": "configure_channels",
    "playlists": "configure_playlists",
    "videos": "configure_videos",
    "whitelist": "configure_whitelist",
    "blacklist": "configure_blacklist",
    "history": "configure_history",
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


def _format_history_timestamp(value: Any) -> str:
    display_value = value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            display_value = dt.astimezone(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            )
    return str(display_value)


def _history_state(event_type: str, metadata: Mapping[str, Any]) -> dict[str, str] | None:
    if event_type == "video.favorite.toggle":
        is_favorite = bool(metadata.get("favorite"))
        return {
            "icon": "★" if is_favorite else "☆",
            "label": "Favorite" if is_favorite else "Not favorite",
            "class": "history-favorite-state",
        }
    if event_type == "video.flag.toggle":
        is_flagged = bool(metadata.get("flagged"))
        return {
            "icon": "⚑" if is_flagged else "⚐",
            "label": "Flagged" if is_flagged else "Not flagged",
            "class": "history-flag-state" if is_flagged else "history-flag-state off",
        }
    return None


def _history_video_info(
    event_type: str, metadata: Mapping[str, Any], app: FastAPI, video_map: Mapping[str, Any]
) -> dict[str, str] | None:
    if event_type not in HISTORY_VIDEO_EVENT_TYPES:
        return None

    video_id = metadata.get("video_id")
    if not isinstance(video_id, str) or not video_id:
        return None

    video_record = video_map.get(video_id)
    title: str | None = None
    if isinstance(video_record, Mapping):
        raw_title = video_record.get("title")
        if isinstance(raw_title, str):
            title = raw_title
    if not title:
        raw_title = metadata.get("video_title")
        if isinstance(raw_title, str) and raw_title.strip():
            title = raw_title.strip()
    if not title:
        title = video_id

    return {
        "id": video_id,
        "title": title,
        "url": app.url_path_for("view_resource", section="videos", resource_id=video_id),
    }


def _history_event_context(
    event: Mapping[str, Any],
    app: FastAPI,
    *,
    video_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = event.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    event_type = str(event.get("event_type") or "")
    video_lookup = video_map or {}
    video_info = _history_video_info(event_type, metadata, app, video_lookup)
    state = _history_state(event_type, metadata)
    metadata_json = json.dumps(metadata, indent=2, sort_keys=True)
    summary = ""
    if video_info:
        summary = video_info.get("title", "")
    elif metadata:
        summary = json.dumps(metadata, separators=(", ", ": "), sort_keys=True)
    event_id = event.get("id")
    detail_url = (
        app.url_path_for("configure_history_event", event_id=str(event_id))
        if event_id is not None
        else None
    )
    return {
        "id": event_id,
        "event_type": event_type,
        "created_at": _format_history_timestamp(event.get("created_at")),
        "metadata_json": metadata_json,
        "summary": summary,
        "video": video_info,
        "state": state,
        "detail_url": detail_url,
    }


def _parse_video_filters(params: Mapping[str, Any]) -> VideoFilters:
    filter_keys = {option[0] for option in VIDEO_FILTER_OPTIONS}

    def _get_list(name: str) -> list[str]:
        getter = getattr(params, "getlist", None)
        if callable(getter):
            return getter(name)
        value = params.get(name)
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value]
        return [str(value)]

    include = {value for value in _get_list("include") if value in filter_keys}
    exclude = {value for value in _get_list("exclude") if value in filter_keys}
    include_channels = {value for value in _get_list("include_channel") if value}
    exclude_channels = {value for value in _get_list("exclude_channel") if value}

    return VideoFilters(
        include=include,
        exclude=exclude,
        include_channels=include_channels,
        exclude_channels=exclude_channels,
    )


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


def _videos_overview_content(videos: list[dict[str, Any]]) -> list[dict[str, str | None]]:
    items: list[dict[str, str | None]] = []
    for video in videos:
        video_id = video.get("id") or ""
        if not video_id:
            continue
        title = video.get("title") or video_id
        channel_title_value = video.get("channel_title")
        channel_title = (
            channel_title_value.strip()
            if isinstance(channel_title_value, str) and channel_title_value.strip()
            else "Unknown channel"
        )
        vote = _resource_vote(video.get("label"))
        encoded_id = quote(video_id, safe="")
        has_raw_value = video.get("has_raw")
        has_raw = bool(has_raw_value)
        if not has_raw and "raw_json" in video:
            has_raw = bool(video.get("raw_json"))
        resource_url = f"/configure/videos/{encoded_id}"
        disqualifying_attributes = video.get("disqualifying_attributes")
        flagged_disqualifier = (
            isinstance(disqualifying_attributes, list)
            and "flagged" in disqualifying_attributes
        )
        items.append(
            {
                "video_id": video_id,
                "channel_title": channel_title,
                "video_title": title,
                "url": resource_url,
                "play_url": f"/?play={encoded_id}",
                "raw_url": f"{resource_url}/raw" if has_raw else None,
                "load_url": f"{resource_url}/load",
                "vote": vote,
                "flagged_disqualifier": flagged_disqualifier,
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

    approved_total: int | None = None
    if list_slug == "whitelist":
        approved_total = _count_approved_videos(videos)

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
        "approved_total": approved_total,
    }


def _listed_resources_content(
    list_slug: str,
    resources: list[dict[str, Any]],
    regenerate_url: str,
    approved_total: int | None = None,
) -> dict[str, Any]:
    heading_label = f"{LIST_PAGE_LABELS.get(list_slug, list_slug.title())} Resources"

    items: list[dict[str, str]] = []
    for resource in resources:
        resource_type = resource.get("resource_type") or ""
        resource_id = resource.get("resource_id") or ""
        if not resource_type or not resource_id:
            continue
        icon = RESOURCE_TYPE_ICONS.get(resource_type, "❓")
        title = resource.get("title") or resource_id
        encoded_resource_id = quote(resource_id, safe="")
        resource_url = f"/configure/{resource_type}s/{encoded_resource_id}"
        items.append(
            {
                "title": title,
                "resource_id": resource_id,
                "icon": icon,
                "type_label": resource_type.title(),
                "url": resource_url,
            }
        )

    return {
        "heading_label": heading_label,
        "regenerate_url": regenerate_url,
        "resources": items,
        "approved_total": approved_total,
    }


def _settings_content(
    settings: dict[str, str],
    save_url: str,
    pair_url: str,
    lounge_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
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
        "item_entries": [],
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
            playlist_context["item_entries"].append(
                {
                    "title": title,
                    "video_id": str(video_id),
                    "video_url": f"/configure/videos/{encoded_video_id}",
                }
            )
        else:
            playlist_context["item_entries"].append(
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


def _count_approved_videos(videos: list[dict[str, Any]]) -> int:
    return sum(
        1
        for video in videos
        if not video.get("blacklisted_by")
        and not video.get("disqualifying_attributes")
    )


def _video_resource_content(
    video: dict | None,
    listed_video: dict[str, Any] | None = None,
    reference_map: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    if not video:
        return {"video": None}

    video_identifier = video.get("id") or ""
    encoded_video_id = quote(video_identifier, safe="") if video_identifier else ""

    disqualifying_attributes = (
        listed_video.get("disqualifying_attributes")
        if isinstance(listed_video, dict)
        else []
    )
    flagged_disqualifier = (
        isinstance(disqualifying_attributes, list)
        and "flagged" in disqualifying_attributes
    )

    return {
        "video": {
            "id": video_identifier,
            "title": video.get("title") or "Untitled video",
            "description": video.get("description") or "",
            "retrieved_at": video.get("retrieved_at") or "Unknown",
            "label": video.get("label"),
            "vote": _resource_vote(video.get("label")),
            "flagged_disqualifier": flagged_disqualifier,
            "disqualifying_attributes": disqualifying_attributes
            if isinstance(disqualifying_attributes, list)
            else [],
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
    video = await fetch_youtube_video_with_bonus(video_id, api_key)
    if not video:
        return None, "No video information available for that ID."
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
        *,
        play_video_id: str | None = None,
        page_title: str = "MyTube Remote",
        videos_api_url: str | None = None,
        menu_active: str = "home",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            "home.html",
            {
                "request": request,
                "page_title": page_title,
                "menu_active": menu_active,
                "videos_api_url": videos_api_url
                or app.url_path_for("list_random_videos"),
                "play_api_url": app.url_path_for("play_video"),
                "pause_api_url": app.url_path_for("pause_video"),
                "resume_api_url": app.url_path_for("resume_video"),
                "favorite_api_url": app.url_path_for(
                    "toggle_favorite", video_id="__VIDEO_ID__"
                ),
                "flag_api_url": app.url_path_for(
                    "toggle_flag", video_id="__VIDEO_ID__"
                ),
                "play_video_id": play_video_id or "",
            },
        )

    async def _record_history_event(
        event_type: str, metadata: dict[str, Any] | None = None
    ) -> None:
        await run_in_threadpool(
            log_history_event,
            event_type,
            metadata or {},
        )

    @app.get("/", response_class=HTMLResponse)
    async def home(
        request: Request,
        play: str | None = Query(default=None, description="Video ID to play immediately."),
    ) -> HTMLResponse:
        play_video_id = (play or "").strip()
        if not play_video_id:
            play_video_id = None
        return _render(request, play_video_id=play_video_id, menu_active="home")

    @app.get("/favorites", response_class=HTMLResponse, name="favorites")
    async def favorites(request: Request) -> HTMLResponse:
        return _render(
            request,
            page_title="MyTube Favorites",
            videos_api_url=app.url_path_for("list_favorite_videos"),
            menu_active="favorites",
        )

    async def _fetch_video_state_flags(
        video_ids: list[str],
    ) -> tuple[dict[str, bool], dict[str, bool]]:
        if not video_ids:
            return {}, {}

        favorite_task = run_in_threadpool(
            fetch_resource_labels_map,
            "video",
            video_ids,
            {"favorite"},
        )
        flagged_task = run_in_threadpool(
            fetch_resource_labels_map,
            "video",
            video_ids,
            {"flagged"},
        )

        favorite_map, flagged_map = await asyncio.gather(
            favorite_task, flagged_task
        )
        favorite_flags = {
            video_id: favorite_map.get(video_id) == "favorite"
            for video_id in video_ids
        }
        flagged_flags = {
            video_id: flagged_map.get(video_id) == "flagged"
            for video_id in video_ids
        }
        return favorite_flags, flagged_flags

    async def _build_video_options(
        entries: list[dict[str, Any]], *, limit: int = 5
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        shuffled_entries = list(entries)
        random.shuffle(shuffled_entries)

        video_options: list[dict[str, Any]] = []
        video_ids: list[str] = []
        for entry in shuffled_entries:
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
                    "favorite": False,
                    "flagged": False,
                }
            )
            video_ids.append(video_id)

        if not video_options:
            return video_options

        favorite_flags, flagged_flags = await _fetch_video_state_flags(video_ids)
        for option in video_options:
            video_id = option.get("video_id")
            if isinstance(video_id, str):
                option["favorite"] = favorite_flags.get(video_id, False)
                option["flagged"] = flagged_flags.get(video_id, False)

        return video_options

    async def _random_video_options(limit: int = 5) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        listed_videos = await run_in_threadpool(fetch_listed_videos, "whitelist")
        approved_videos = [
            video
            for video in listed_videos
            if video.get("video_id")
            and not video.get("blacklisted_by")
            and not video.get("disqualifying_attributes")
        ]
        if not approved_videos:
            return []

        return await _build_video_options(approved_videos, limit=limit)

    async def _favorite_video_options(limit: int = 5) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        listed_videos = await run_in_threadpool(fetch_listed_videos, "whitelist")
        approved_videos = [
            video
            for video in listed_videos
            if video.get("video_id")
            and not video.get("blacklisted_by")
            and not video.get("disqualifying_attributes")
        ]

        video_ids = [entry.get("video_id") for entry in approved_videos if entry.get("video_id")]
        if not video_ids:
            return []

        favorite_map = await run_in_threadpool(
            fetch_resource_labels_map, "video", video_ids, {"favorite"}
        )
        favorite_entries = [
            entry
            for entry in approved_videos
            if entry.get("video_id")
            and favorite_map.get(entry.get("video_id")) == "favorite"
        ]

        return await _build_video_options(favorite_entries, limit=limit)

    @app.get("/videos/random", name="list_random_videos")
    async def list_random_videos(limit: int = Query(default=5, ge=1, le=20)) -> dict[str, Any]:
        """Return a random selection of approved videos."""

        videos = await _random_video_options(limit=limit)
        await _record_history_event(
            "videos.random",
            {
                "limit": limit,
                "video_ids": [
                    entry.get("video_id")
                    for entry in videos
                    if isinstance(entry.get("video_id"), str)
                ],
            },
        )
        return {"videos": videos}

    @app.get("/videos/favorites", name="list_favorite_videos")
    async def list_favorite_videos(
        limit: int = Query(default=5, ge=1, le=20)
    ) -> dict[str, Any]:
        """Return a random selection of favorited videos."""

        videos = await _favorite_video_options(limit=limit)
        await _record_history_event(
            "videos.favorites",
            {
                "limit": limit,
                "video_ids": [
                    entry.get("video_id")
                    for entry in videos
                    if isinstance(entry.get("video_id"), str)
                ],
            },
        )
        return {"videos": videos}

    @app.post("/videos/{video_id}/favorite", name="toggle_favorite")
    async def toggle_favorite(video_id: str) -> dict[str, Any]:
        normalized_id = video_id.strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="Video ID is required")

        favorite_label = await run_in_threadpool(
            fetch_resource_label,
            "video",
            normalized_id,
            {"favorite"},
        )
        is_favorite = favorite_label == "favorite"

        if is_favorite:
            await run_in_threadpool(
                clear_resource_label,
                "video",
                normalized_id,
                "favorite",
            )
        else:
            await run_in_threadpool(
                set_resource_label,
                "video",
                normalized_id,
                "favorite",
            )

        await _record_history_event(
            "video.favorite.toggle",
            {"video_id": normalized_id, "favorite": not is_favorite},
        )

        return {"video_id": normalized_id, "favorite": not is_favorite}

    @app.post("/videos/{video_id}/flag", name="toggle_flag")
    async def toggle_flag(video_id: str) -> dict[str, Any]:
        normalized_id = video_id.strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="Video ID is required")

        flag_label = await run_in_threadpool(
            fetch_resource_label,
            "video",
            normalized_id,
            {"flagged"},
        )
        is_flagged = flag_label == "flagged"

        if is_flagged:
            await run_in_threadpool(
                clear_resource_label,
                "video",
                normalized_id,
                "flagged",
            )
        else:
            await run_in_threadpool(
                set_resource_label,
                "video",
                normalized_id,
                "flagged",
            )

        await _record_history_event(
            "video.flag.toggle",
            {"video_id": normalized_id, "flagged": not is_flagged},
        )

        await run_in_threadpool(
            refresh_listed_video_disqualifications, normalized_id
        )

        return {"video_id": normalized_id, "flagged": not is_flagged}

    @app.post("/lounge/play", name="play_video")
    async def play_video_handler(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc

        video_id: str = ""
        if isinstance(payload, Mapping):
            raw_video_id = payload.get("video_id")
            if isinstance(raw_video_id, str):
                video_id = raw_video_id.strip()
            elif raw_video_id is not None:
                video_id = str(raw_video_id).strip()

        if not video_id:
            raise HTTPException(status_code=400, detail="Video ID is required.")

        lounge_manager_dep = getattr(request.app.state, "lounge", None)
        if not isinstance(lounge_manager_dep, LoungeManager):
            raise HTTPException(
                status_code=503,
                detail="TV lounge controller is not available.",
            )

        settings = await run_in_threadpool(fetch_settings, ["youtube_app_auth"])
        auth_state = _load_lounge_auth(settings)
        if not auth_state:
            raise HTTPException(
                status_code=503,
                detail="TV is not paired with the YouTube app.",
            )

        screen_id = auth_state.get("screenId")
        if not isinstance(screen_id, str) or not screen_id:
            raise HTTPException(
                status_code=503,
                detail="TV is not paired with the YouTube app.",
            )

        await _record_history_event(
            "lounge.play",
            {"video_id": video_id, "screen_id": screen_id},
        )

        try:
            controller = await lounge_manager_dep.get(screen_id)
            if controller is None:
                controller = await lounge_manager_dep.upsert_from_auth(
                    auth_state, name=LOUNGE_REMOTE_NAME
                )
            await controller.play_video(video_id)
        except HTTPException:
            raise
        except (PairingError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Unable to start playback for video %s on screen %s: %s",
                video_id,
                screen_id,
                exc,
                exc_info=True,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Unexpected error while attempting to play %s on screen %s: %s",
                video_id,
                screen_id,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail="Unable to start playback on the YouTube app.",
            ) from exc

        video_record, _ = await _load_video_record(video_id)
        playing_context: dict[str, Any]
        if video_record and video_record.get("raw_json"):
            playing_context = _build_playing_context(video_record, video_id)
        else:
            playing_context = {
                "title": video_id,
                "thumbnail_url": None,
                "video_id": video_id,
            }

        favorite_label_task = run_in_threadpool(
            fetch_resource_label,
            "video",
            video_id,
            {"favorite"},
        )
        flagged_label_task = run_in_threadpool(
            fetch_resource_label,
            "video",
            video_id,
            {"flagged"},
        )
        favorite_label, flagged_label = await asyncio.gather(
            favorite_label_task, flagged_label_task
        )
        playing_context["favorite"] = favorite_label == "favorite"
        playing_context["flagged"] = flagged_label == "flagged"

        return {"playing": playing_context}

    @app.post("/lounge/pause", name="pause_video")
    async def pause_video_handler(request: Request) -> dict[str, Any]:
        lounge_manager_dep = getattr(request.app.state, "lounge", None)
        if not isinstance(lounge_manager_dep, LoungeManager):
            raise HTTPException(
                status_code=503,
                detail="TV lounge controller is not available.",
            )

        settings = await run_in_threadpool(fetch_settings, ["youtube_app_auth"])
        auth_state = _load_lounge_auth(settings)
        if not auth_state:
            raise HTTPException(
                status_code=503,
                detail="TV is not paired with the YouTube app.",
            )

        screen_id = auth_state.get("screenId")
        if not isinstance(screen_id, str) or not screen_id:
            raise HTTPException(
                status_code=503,
                detail="TV is not paired with the YouTube app.",
            )

        await _record_history_event(
            "lounge.pause",
            {"screen_id": screen_id},
        )

        try:
            controller = await lounge_manager_dep.get(screen_id)
            if controller is None:
                controller = await lounge_manager_dep.upsert_from_auth(
                    auth_state, name=LOUNGE_REMOTE_NAME
                )
            await controller.pause()
        except HTTPException:
            raise
        except (PairingError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Unable to pause playback on screen %s: %s",
                screen_id,
                exc,
                exc_info=True,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Unexpected error while attempting to pause playback on screen %s: %s",
                screen_id,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail="Unable to pause playback on the YouTube app.",
            ) from exc

        return {"status": "paused"}

    @app.post("/lounge/resume", name="resume_video")
    async def resume_video_handler(request: Request) -> dict[str, Any]:
        lounge_manager_dep = getattr(request.app.state, "lounge", None)
        if not isinstance(lounge_manager_dep, LoungeManager):
            raise HTTPException(
                status_code=503,
                detail="TV lounge controller is not available.",
            )

        settings = await run_in_threadpool(fetch_settings, ["youtube_app_auth"])
        auth_state = _load_lounge_auth(settings)
        if not auth_state:
            raise HTTPException(
                status_code=503,
                detail="TV is not paired with the YouTube app.",
            )

        screen_id = auth_state.get("screenId")
        if not isinstance(screen_id, str) or not screen_id:
            raise HTTPException(
                status_code=503,
                detail="TV is not paired with the YouTube app.",
            )

        await _record_history_event(
            "lounge.resume",
            {"screen_id": screen_id},
        )

        try:
            controller = await lounge_manager_dep.get(screen_id)
            if controller is None:
                controller = await lounge_manager_dep.upsert_from_auth(
                    auth_state, name=LOUNGE_REMOTE_NAME
                )
            await controller.resume()
        except HTTPException:
            raise
        except (PairingError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Unable to resume playback on screen %s: %s",
                screen_id,
                exc,
                exc_info=True,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Unexpected error while attempting to resume playback on screen %s: %s",
                screen_id,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail="Unable to resume playback on the YouTube app.",
            ) from exc

        return {"status": "playing"}

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
        filters = _parse_video_filters(request.query_params)
        channels = await run_in_threadpool(fetch_all_channels)
        videos = await run_in_threadpool(fetch_all_videos, filters)
        video_items = _videos_overview_content(videos)
        heading = f"Manage {RESOURCE_LABELS['videos']}"
        filter_channels = sorted(
            (
                {
                    "id": channel.get("id"),
                    "title": channel.get("title") or channel.get("id"),
                }
                for channel in channels
                if channel.get("id")
            ),
            key=lambda item: (item.get("title") or "").casefold(),
        )
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="videos",
            template_name="configure/videos_overview.html",
            context={
                "videos": video_items,
                "video_filters": filters,
                "video_filter_options": VIDEO_FILTER_OPTIONS,
                "filter_channels": filter_channels,
            },
            form_action=app.url_path_for(CREATE_ROUTE_NAMES["videos"]),
        )

    @app.get(
        "/configure/whitelist",
        response_class=HTMLResponse,
        name="configure_whitelist",
    )
    async def configure_whitelist(request: Request) -> HTMLResponse:
        resources = await run_in_threadpool(fetch_labeled_resources, "whitelisted")
        videos = await run_in_threadpool(fetch_listed_videos, "whitelist")
        approved_total = _count_approved_videos(videos)
        regenerate_url = app.url_path_for(
            "regenerate_listed_videos", list_slug="whitelist"
        )
        list_context = _listed_resources_content(
            "whitelist", resources, regenerate_url, approved_total
        )
        heading = f"{LIST_PAGE_LABELS['whitelist']} Resources"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="whitelist",
            template_name="configure/listed_resources.html",
            context=list_context,
            show_resource_form=False,
        )

    @app.get(
        "/configure/blacklist",
        response_class=HTMLResponse,
        name="configure_blacklist",
    )
    async def configure_blacklist(request: Request) -> HTMLResponse:
        resources = await run_in_threadpool(fetch_labeled_resources, "blacklisted")
        regenerate_url = app.url_path_for(
            "regenerate_listed_videos", list_slug="blacklist"
        )
        list_context = _listed_resources_content(
            "blacklist", resources, regenerate_url
        )
        heading = f"{LIST_PAGE_LABELS['blacklist']} Resources"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="blacklist",
            template_name="configure/listed_resources.html",
            context=list_context,
            show_resource_form=False,
        )

    @app.get(
        "/configure/history",
        response_class=HTMLResponse,
        name="configure_history",
    )
    async def configure_history(request: Request) -> HTMLResponse:
        events = await run_in_threadpool(fetch_history, 20)
        video_ids: set[str] = set()
        for event in events:
            metadata = event.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            event_type = event.get("event_type")
            video_id = metadata.get("video_id")
            if (
                isinstance(video_id, str)
                and video_id
                and isinstance(event_type, str)
                and event_type in HISTORY_VIDEO_EVENT_TYPES
            ):
                video_ids.add(video_id)

        video_map: dict[str, Any] = {}
        for video_id in video_ids:
            video_map[video_id] = await run_in_threadpool(fetch_video, video_id)

        formatted_events = [
            _history_event_context(event, app, video_map=video_map) for event in events
        ]

        return _render_config_page(
            request,
            app,
            heading="Viewing History",
            active_section="history",
            template_name="configure/history.html",
            context={"events": formatted_events},
            show_resource_form=False,
        )

    @app.get(
        "/configure/history/{event_id}",
        response_class=HTMLResponse,
        name="configure_history_event",
    )
    async def configure_history_event(request: Request, event_id: int) -> HTMLResponse:
        event = await run_in_threadpool(fetch_history_event, event_id)
        if not event:
            raise HTTPException(status_code=404, detail="History event not found")

        video_map: dict[str, Any] = {}
        metadata = event.get("metadata")
        if isinstance(metadata, Mapping):
            video_id = metadata.get("video_id")
            if isinstance(video_id, str) and video_id:
                video_map[video_id] = await run_in_threadpool(fetch_video, video_id)

        formatted_event = _history_event_context(event, app, video_map=video_map)

        return _render_config_page(
            request,
            app,
            heading="History Event",
            active_section="history",
            template_name="configure/history_event.html",
            context={"event": formatted_event},
            show_resource_form=False,
        )

    @app.get(
        "/configure/settings",
        response_class=HTMLResponse,
        name="configure_settings",
    )
    async def configure_settings(request: Request) -> HTMLResponse:
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
            settings, save_url, pair_url, lounge_status
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
        video_data = await fetch_youtube_video_with_bonus(
            stripped_resource_id, api_key
        )
        if not video_data:
            raise HTTPException(status_code=404, detail="Video not found")
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
        "/configure/videos/{resource_id}/load",
        name="load_video_resource",
    )
    async def load_video_resource(resource_id: str) -> Response:
        normalized_id = resource_id.strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="Resource ID is required")

        video_record, error_message = await _load_video_record(normalized_id)
        if not video_record:
            detail = error_message or "Unable to load video details."
            raise HTTPException(status_code=502, detail=detail)

        redirect_url = app.url_path_for(
            "view_resource",
            section="videos",
            resource_id=normalized_id,
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

    return app
