"""Helpers for interacting with the YouTube Data API."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool


logger = logging.getLogger(__name__)


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

    all_items: list[dict[str, Any]] = []
    combined_data: dict[str, Any] | None = None
    next_page_token: str | None = None
    page_count = 0
    url = ""

    while True:
        page_params = dict(params)
        if next_page_token:
            page_params["pageToken"] = next_page_token

        url, data = await run_in_threadpool(
            _youtube_api_request, "playlistItems", page_params
        )

        page_count += 1
        page_items = data.get("items") or []
        all_items.extend(page_items)

        if combined_data is None:
            combined_data = data
        else:
            existing_items = combined_data.get("items")
            if isinstance(existing_items, list):
                existing_items.extend(page_items)
            else:
                combined_data["items"] = list(all_items)

        next_page_token_value = data.get("nextPageToken")
        next_page_token = (
            next_page_token_value if isinstance(next_page_token_value, str) else None
        )

        if not next_page_token:
            break

        if page_count >= 40:
            logger.warning(
                "Stopped fetching playlistItems for playlist %s after %s pages due to limit.",
                playlist_id,
                page_count,
            )
            next_page_token = None
            break

    if combined_data is None:
        combined_data = {"items": []}
    else:
        combined_data["items"] = list(all_items)
        page_info = combined_data.get("pageInfo")
        if isinstance(page_info, dict):
            page_info["totalResults"] = len(all_items)
        combined_data.pop("nextPageToken", None)

    return url, combined_data


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

