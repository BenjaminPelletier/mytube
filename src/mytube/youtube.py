"""Helpers for interacting with the YouTube Data API."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool


def load_youtube_api_key() -> str:
    """Load the YouTube API key from the environment or helper file."""

    key = os.environ.get("YOUTUBE_API_KEY")
    if key:
        stripped = key.strip()
        if stripped:
            return stripped

    key_path = Path.cwd() / ".youtube-apikey"
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


async def fetch_youtube_channel_sections(
    channel_id: str, api_key: str
) -> tuple[str, dict[str, Any]]:
    """Fetch channel sections for a YouTube channel."""

    params = {
        "part": "snippet,contentDetails",
        "channelId": channel_id,
        "maxResults": "50",
        "key": api_key,
    }
    return await run_in_threadpool(
        _youtube_api_request, "channelSections", params
    )


async def fetch_youtube_playlists(
    playlist_ids: Iterable[str], api_key: str
) -> list[dict[str, Any]]:
    """Fetch metadata for multiple YouTube playlists."""

    ids = [playlist_id for playlist_id in playlist_ids if playlist_id]
    if not ids:
        return []

    items: list[dict[str, Any]] = []
    chunk_size = 50  # Maximum number of playlist IDs per API call
    for index in range(0, len(ids), chunk_size):
        chunk = ids[index : index + chunk_size]
        params = {
            "part": "snippet,contentDetails",
            "id": ",".join(chunk),
            "key": api_key,
        }
        _, data = await run_in_threadpool(_youtube_api_request, "playlists", params)
        items.extend(data.get("items") or [])
    return items


async def fetch_youtube_playlist_items(
    playlist_id: str, api_key: str
) -> tuple[str, dict[str, Any]]:
    """Fetch the items contained in a YouTube playlist."""

    params = {
        "part": "snippet,contentDetails",
        "playlistId": playlist_id,
        "maxResults": "50",
        "key": api_key,
    }
    return await run_in_threadpool(_youtube_api_request, "playlistItems", params)


async def fetch_youtube_channels(
    resource_id: str, api_key: str
) -> tuple[str, dict[str, Any]]:
    """Fetch channel data for a YouTube channel ID or handle."""

    params: dict[str, str] = {
        "part": "snippet,statistics,contentDetails",
        "key": api_key,
    }
    if resource_id.startswith("@"):
        params["forHandle"] = resource_id[1:]
    else:
        params["id"] = resource_id
    return await run_in_threadpool(_youtube_api_request, "channels", params)


async def fetch_youtube_videos(
    video_id: str, api_key: str
) -> tuple[str, dict[str, Any]]:
    """Fetch video data for a YouTube video."""

    params = {
        "part": "snippet,contentDetails,statistics",
        "id": video_id,
        "key": api_key,
    }
    return await run_in_threadpool(_youtube_api_request, "videos", params)


__all__ = [
    "fetch_youtube_channel_sections",
    "fetch_youtube_channels",
    "fetch_youtube_playlist_items",
    "fetch_youtube_playlists",
    "fetch_youtube_videos",
    "load_youtube_api_key",
]

