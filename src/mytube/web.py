"""FastAPI application that exposes a Chromecast remote."""

from __future__ import annotations

import html
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
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
from .ytlounge import PairingError, dumps_auth_payload, pair_with_link_code
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
    content: str,
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
    return templates.TemplateResponse(
        "configure.html",
        {
            "request": request,
            "heading": heading,
            "navigation": navigation,
            "form_action": action,
            "resource_value": resource_value,
            "content_html": content,
            "show_resource_form": show_resource_form,
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
        vote = _resource_vote(channel.get("label"))
        encoded_id = quote(channel_id, safe="")
        link = f"<a href=\"/configure/channels/{encoded_id}\">{html.escape(title)}</a>"
        vote_display = f" {vote}" if vote else ""
        items.append(
            "<li>"
            f"{link}{vote_display}"
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
        vote = _resource_vote(video.get("label"))
        encoded_id = quote(video_id, safe="")
        link = f"<a href=\"/configure/videos/{encoded_id}\">{html.escape(title)}</a>"
        vote_display = f" {vote}" if vote else ""
        items.append(
            "<li>"
            f"{link}{vote_display}"
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


def _listed_videos_content(
    list_slug: str, videos: list[dict[str, Any]], regenerate_url: str
) -> str:
    if list_slug not in LIST_PAGE_FIELDS:
        raise HTTPException(status_code=404, detail="Unknown list page")

    heading_label = LIST_PAGE_LABELS.get(list_slug, list_slug.title())
    primary_field = LIST_PAGE_FIELDS[list_slug]
    secondary_field = (
        "blacklisted_by" if primary_field == "whitelisted_by" else "whitelisted_by"
    )

    button_html = (
        f"<form class=\"regenerate-form\" method=\"post\" action=\"{html.escape(regenerate_url)}\">"
        "<button type=\"submit\">Regenerate</button>"
        "</form>"
    )

    if not videos:
        return (
            "<section>"
            f"<h2>{heading_label} Videos</h2>"
            f"{button_html}"
            "<p>No videos have been recorded yet.</p>"
            "</section>"
        )

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

    items: list[str] = []
    for video in videos:
        video_id = video.get("video_id") or ""
        if not video_id:
            continue
        title = video.get("title") or video_id
        encoded_video_id = quote(video_id, safe="")
        link = (
            f"<a href=\"/configure/videos/{encoded_video_id}\">{html.escape(title)}</a>"
        )
        listed_lines = _format_listed_lines(video, reference_map)
        identifiers_html = listed_lines or ""
        items.append(
            "<li>"
            f"{link}"
            f"{identifiers_html}"
            "</li>"
        )

    items_html = "".join(items)
    return (
        "<section>"
        f"<h2>{heading_label} Videos</h2>"
        f"{button_html}"
        "<ol>"
        f"{items_html}"
        "</ol>"
        "</section>"
    )


def _settings_content(
    devices_url: str,
    settings: dict[str, str],
    save_url: str,
    pair_url: str,
) -> str:
    devices_endpoint = json.dumps(devices_url)
    pair_endpoint = json.dumps(pair_url)
    settings_json = json.dumps(settings or {})
    escaped_save_url = html.escape(save_url, quote=True)
    return (
        "<section class=\"settings-section\">"
        "<h2>Playback Settings</h2>"
        "<p>Choose the Chromecast device MyTube should prioritize when casting.</p>"
        f"<form class=\"settings-form\" method=\"post\" action=\"{escaped_save_url}\">"
        "<div class=\"settings-group\">"
        "<label class=\"settings-label\" for=\"preferred-device\">Preferred casting device</label>"
        "<select id=\"preferred-device\" name=\"preferred_device\" class=\"settings-select\" disabled>"
        "<option>Loading devices...</option>"
        "</select>"
        "</div>"
        "<div class=\"settings-actions\">"
        "<button type=\"submit\" class=\"settings-save-button\">Save Settings</button>"
        "</div>"
        "</form>"
        "</section>"
        "<section class=\"settings-section\">"
        "<h2>YouTube App Pairing</h2>"
        "<p>Link MyTube with the YouTube app to cast directly from your TV.</p>"
        "<form id=\"youtube-pair-form\" class=\"pair-form\" novalidate>"
        "<label class=\"pair-label\" for=\"youtube-link-code\">Link with TV code</label>"
        "<div class=\"pair-fields\">"
        "<input id=\"youtube-link-code\" name=\"link_code\" class=\"pair-input\" type=\"text\" autocomplete=\"off\" placeholder=\"XXX XXX XXX XXX\" required>"
        "<button type=\"submit\" id=\"youtube-pair-button\" class=\"pair-button\">Pair</button>"
        "</div>"
        "<p class=\"settings-help\">Find the code in the YouTube app under Settings → Link with TV code.</p>"
        "</form>"
        "<dialog id=\"settings-error-dialog\" class=\"settings-modal\" aria-labelledby=\"settings-error-title\">"
        "<form method=\"dialog\" class=\"settings-modal-content\">"
        "<h3 id=\"settings-error-title\">Pairing error</h3>"
        "<p id=\"settings-error-message\"></p>"
        "<button type=\"submit\" value=\"close\" class=\"settings-modal-close\">Close</button>"
        "</form>"
        "</dialog>"
        "<script>(function(){"
        "const select=document.getElementById('preferred-device');"
        f"const settings={settings_json};"
        "const preferredRaw=settings && typeof settings.preferred_device==='string'?settings.preferred_device:'';"
        "const preferred=preferredRaw.trim();"
        "const setSingleOption=(label)=>{"
        "if(!select){return;}"
        "select.innerHTML='';"
        "const option=document.createElement('option');"
        "option.textContent=label;"
        "option.value='';"
        "option.disabled=true;"
        "option.selected=true;"
        "select.append(option);"
        "select.disabled=true;"
        "};"
        "const enableSelect=(devices)=>{"
        "if(!select){return;}"
        "select.innerHTML='';"
        "const noneOption=document.createElement('option');"
        "noneOption.textContent='No preferred device';"
        "noneOption.value='';"
        "select.append(noneOption);"
        "let matched=false;"
        "devices.forEach((name)=>{"
        "if(typeof name!=='string'){return;}"
        "const trimmed=name.trim();"
        "if(!trimmed){return;}"
        "const option=document.createElement('option');"
        "option.value=trimmed;"
        "option.textContent=trimmed;"
        "if(!matched && preferred && trimmed===preferred){"
        "option.selected=true;"
        "matched=true;"
        "}"
        "select.append(option);"
        "});"
        "if(!matched){"
        "select.value='';"
        "}"
        "select.disabled=false;"
        "};"
        "const loadDevices=async()=>{"
        "if(!select){return;}"
        "try{"
        f"const response=await fetch({devices_endpoint});"
        "if(!response.ok){throw new Error('Request failed');}"
        "const payload=await response.json();"
        "const devices=Array.isArray(payload.devices)?payload.devices:[];"
        "if(devices.length===0){"
        "setSingleOption('No devices found');"
        "return;"
        "}"
        "enableSelect(devices);"
        "}catch(error){"
        "setSingleOption('Unable to load devices');"
        "}"
        "};"
        "loadDevices();"
        "const pairForm=document.getElementById('youtube-pair-form');"
        "if(!pairForm){return;}"
        "const codeInput=document.getElementById('youtube-link-code');"
        "const pairButton=document.getElementById('youtube-pair-button');"
        "const errorDialog=document.getElementById('settings-error-dialog');"
        "const errorMessage=document.getElementById('settings-error-message');"
        f"const pairEndpoint={pair_endpoint};"
        "const existingAuth=settings && typeof settings.youtube_app_auth==='string'?settings.youtube_app_auth.trim():'';"
        "const setButtonState=(label, disabled)=>{"
        "if(pairButton){"
        "pairButton.textContent=label;"
        "pairButton.disabled=!!disabled;"
        "}"
        "};"
        "const showError=(message)=>{"
        "const fallback=message && typeof message==='string'?message:'Unable to pair with the YouTube app.';"
        "if(errorDialog && typeof errorDialog.showModal==='function'){"
        "errorMessage.textContent=fallback;"
        "try{errorDialog.showModal();}catch(showError){window.alert(fallback);}"
        "}else{window.alert(fallback);}"
        "};"
        "if(existingAuth){"
        "setButtonState('Paired!', true);"
        "if(codeInput){codeInput.disabled=true;}"
        "}"
        "pairForm.addEventListener('submit', async(event)=>{"
        "event.preventDefault();"
        "if(!codeInput || !pairButton){return;}"
        "const code=codeInput.value.trim();"
        "if(!code){"
        "showError('Enter the code displayed on your TV.');"
        "codeInput.focus();"
        "return;"
        "}"
        "const originalLabel=pairButton.dataset.originalLabel||pairButton.textContent||'Pair';"
        "pairButton.dataset.originalLabel=originalLabel;"
        "setButtonState('Pairing...', true);"
        "try{"
        "const response=await fetch(pairEndpoint,{"
        "method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({code})"
        "});"
        "if(!response.ok){"
        "let detail='Unable to pair with the YouTube app.';"
        "try{"
        "const payload=await response.json();"
        "if(payload && typeof payload.detail==='string'){detail=payload.detail;}"
        "}catch(ignore){}"
        "throw new Error(detail);"
        "}"
        "setButtonState('Paired!', true);"
        "if(codeInput){codeInput.disabled=true;}"
        "}catch(error){"
        "setButtonState(pairButton.dataset.originalLabel||'Pair', false);"
        "showError(error && typeof error.message==='string'?error.message:'Unable to pair with the YouTube app.');"
        "}"
        "});"
        "})();</script>"
        "</section>"
    )


def _pair_and_store(code: str) -> dict[str, Any]:
    """Pair with the YouTube app and persist the auth payload."""

    payload = pair_with_link_code(code)
    json_payload = dumps_auth_payload(payload)
    store_settings({"youtube_app_auth": json_payload})
    return payload


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


def _channel_resource_content(
    channel: dict | None,
    sections: list[dict],
    playlist_map: dict[str, dict] | None = None,
) -> str:
    if not channel:
        return (
            "<section>"
            "<h2>Channel Not Found</h2>"
            "<p>No stored data is available for this channel. Submit the identifier "
            "through the form to fetch it from YouTube.</p>"
            "</section>"
        )

    channel_identifier = channel.get("id") or ""
    channel_id = html.escape(channel_identifier)
    encoded_channel_id = quote(channel_identifier, safe="")
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

    playlist_map = playlist_map or {}
    section_items: list[str] = []
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
            playlist_record = playlist_map.get(playlist_id)
            playlist_title = (
                playlist_record.get("title") if isinstance(playlist_record, dict) else None
            )
            display_text = playlist_title or playlist_id
            encoded_playlist_id = quote(playlist_id, safe="")
            display_html = (
                f"<a href=\"/configure/playlists/{encoded_playlist_id}\">"
                f"{html.escape(display_text)}"
                "</a>"
            )
        else:
            title_text = section.get("title") if isinstance(section, dict) else None
            if not title_text:
                snippet = raw.get("snippet") if isinstance(raw, dict) else {}
                if isinstance(snippet, dict):
                    title_text = snippet.get("title")
            if not title_text and isinstance(section, dict):
                title_text = section.get("id")
            title_text = title_text or "Untitled"
            display_html = html.escape(title_text)

        section_items.append(f"<li>{display_html}</li>")

    if section_items:
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

    vote_display = f" {vote}" if vote else ""
    info_link = (
        ""
        if not encoded_channel_id
        else (
            " <a class=\"resource-info-link\" "
            "href=\"/configure/channels/"
            f"{encoded_channel_id}/raw\" "
            "title=\"View raw YouTube data\" "
            "aria-label=\"View raw YouTube data\">🛈</a>"
        )
    )

    heading = (
        "<h2 class=\"resource-title\">"
        f"<span class=\"resource-title-text\">{title}{vote_display}</span>"
        f"{info_link}"
        "</h2>"
    )

    return (
        "<section>"
        f"{heading}"
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
    encoded_playlist_id = quote(str(playlist_id), safe="")
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
        info_link = (
            ""
            if not encoded_playlist_id
            else (
                " <a class=\"resource-info-link\" "
                "href=\"/configure/playlists/"
                f"{encoded_playlist_id}/raw\" "
                "title=\"View raw YouTube data\" "
                "aria-label=\"View raw YouTube data\">🛈</a>"
            )
        )
        heading = (
            "<h2 class=\"resource-title\">"
            f"<span class=\"resource-title-text\">{title}</span>"
            f"{info_link}"
            "</h2>"
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


def _format_listed_line(
    prefix: str,
    identifiers: list[str],
    reference_map: dict[str, dict[str, str]],
) -> str:
    items: list[str] = []
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        normalized = identifier.strip()
        if not normalized:
            continue
        info = reference_map.get(normalized)
        if info:
            title_text = html.escape(info.get("title") or normalized)
            url = info.get("url") or f"/configure/videos/{quote(normalized, safe='')}"
        else:
            title_text = html.escape(normalized)
            url = f"/configure/videos/{quote(normalized, safe='')}"
        escaped_url = html.escape(url, quote=True)
        items.append(f"<a href=\"{escaped_url}\">{title_text}</a>")

    if not items:
        return ""

    joined = ", ".join(items)
    return f"<p class=\"resource-vote-line\">{prefix} {joined}</p>"


def _format_listed_lines(
    listed_video: dict[str, Any] | None,
    reference_map: dict[str, dict[str, str]] | None,
) -> str:
    if not listed_video:
        return ""

    effective_map = reference_map or {}
    lines: list[str] = []
    for key, icon in (("whitelisted_by", "👍"), ("blacklisted_by", "👎")):
        identifiers = listed_video.get(key)
        if isinstance(identifiers, list):
            line_html = _format_listed_line(icon, identifiers, effective_map)
            if line_html:
                lines.append(line_html)
    return "".join(lines)


def _video_resource_content(
    video: dict | None,
    listed_video: dict[str, Any] | None = None,
    reference_map: dict[str, dict[str, str]] | None = None,
) -> str:
    if not video:
        return (
            "<section>"
            "<h2>Video Not Found</h2>"
            "<p>No stored data is available for this video. Submit the identifier "
            "through the form to fetch it from YouTube.</p>"
            "</section>"
        )

    video_identifier = video.get("id") or ""
    video_id = html.escape(video_identifier)
    encoded_video_id = quote(video_identifier, safe="")
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

    vote_display = f" {vote}" if vote else ""
    info_link = (
        ""
        if not encoded_video_id
        else (
            " <a class=\"resource-info-link\" "
            "href=\"/configure/videos/"
            f"{encoded_video_id}/raw\" "
            "title=\"View raw YouTube data\" "
            "aria-label=\"View raw YouTube data\">🛈</a>"
        )
    )
    heading = (
        "<h2 class=\"resource-title\">"
        f"<span class=\"resource-title-text\">{title}{vote_display}</span>"
        f"{info_link}"
        "</h2>"
    )

    listed_lines_html = _format_listed_lines(listed_video, reference_map)

    return (
        "<section>"
        f"{heading}"
        f"<p><small>ID: {video_id}</small></p>"
        f"{listed_lines_html}"
        f"{description_html}"
        f"<p><strong>Retrieved:</strong> {retrieved_at}</p>"
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
        f"<pre class=\"api-response\"><code>{json_payload}</code></pre>"
        "</section>"
    )


def _raw_payload_content(resource_label: str, resource_id: str, payload: Any) -> str:
    escaped_id = html.escape(resource_id)
    formatted_json = html.escape(json.dumps(payload, indent=2, sort_keys=True))
    return (
        "<section>"
        f"<h2>{resource_label} API response</h2>"
        f"<p><small>ID: {escaped_id}</small></p>"
        f"<pre class=\"raw-json\"><code>{formatted_json}</code></pre>"
        "</section>"
    )


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
        video_options: list[dict[str, str | None]] = []

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

        listed_videos = await run_in_threadpool(fetch_listed_videos, "whitelist")
        approved_videos = [
            video
            for video in listed_videos
            if video.get("video_id") and not video.get("blacklisted_by")
        ]
        random.shuffle(approved_videos)

        for entry in approved_videos:
            if len(video_options) >= 5:
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

        return _render(
            request,
            status=status_message,
            playing=playing_video,
            videos=video_options,
        )

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

    @app.get(
        "/configure/channels",
        response_class=HTMLResponse,
        name="configure_channels",
    )
    async def configure_channels(request: Request) -> HTMLResponse:
        channels = await run_in_threadpool(fetch_all_channels)
        content = _channels_overview_content(channels)
        heading = f"Manage {RESOURCE_LABELS['channels']}"
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
        heading = f"Manage {RESOURCE_LABELS['playlists']}"
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
        heading = f"Manage {RESOURCE_LABELS['videos']}"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="videos",
            content=content,
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
        content = _listed_videos_content("whitelist", videos, regenerate_url)
        heading = f"{LIST_PAGE_LABELS['whitelist']} Videos"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="whitelist",
            content=content,
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
        content = _listed_videos_content("blacklist", videos, regenerate_url)
        heading = f"{LIST_PAGE_LABELS['blacklist']} Videos"
        return _render_config_page(
            request,
            app,
            heading=heading,
            active_section="blacklist",
            content=content,
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
        content = _settings_content(devices_url, settings, save_url, pair_url)
        return _render_config_page(
            request,
            app,
            heading="Application Settings",
            active_section="settings",
            content=content,
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

        try:
            await run_in_threadpool(_pair_and_store, code_value)
        except PairingError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unexpected error while pairing with YouTube app")
            raise HTTPException(
                status_code=502,
                detail="Unable to pair with the YouTube app.",
            ) from exc

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
        content = _raw_payload_content("Channel", resource_id, raw_payload)
        return _render_config_page(
            request,
            app,
            heading="Channel raw data",
            active_section="channels",
            content=content,
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
        content = _raw_payload_content("Playlist", resource_id, raw_payload)
        return _render_config_page(
            request,
            app,
            heading="Playlist raw data",
            active_section="playlists",
            content=content,
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
        content = _raw_payload_content("Video", resource_id, raw_payload)
        return _render_config_page(
            request,
            app,
            heading="Video raw data",
            active_section="videos",
            content=content,
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
        if normalized_section == "playlists":
            playlist_items = await run_in_threadpool(
                fetch_playlist_items, resource_id
            )
            playlist = await run_in_threadpool(fetch_playlist, resource_id)
            content = _playlist_resource_content(
                resource_id, playlist_items, list_choice, playlist
            )
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
            content = _channel_resource_content(channel, channel_sections, playlist_map)
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

            content = _video_resource_content(
                video, listed_video, reference_map
            )
        else:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail="Unsupported configuration section")
        heading = f"{RESOURCE_LABELS[normalized_section]} Resource"
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

