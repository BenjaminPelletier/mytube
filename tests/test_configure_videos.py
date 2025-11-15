"""Tests for video configuration helpers."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("sqlmodel")

from mytube import web


def test_videos_overview_content_includes_channel_title() -> None:
    videos = [
        {
            "id": "video123",
            "title": "Sample Video",
            "channel_title": "Sample Channel",
            "label": None,
            "has_raw": True,
        }
    ]

    items = web._videos_overview_content(videos)  # type: ignore[attr-defined]

    assert items == [
        {
            "video_id": "video123",
            "channel_title": "Sample Channel",
            "video_title": "Sample Video",
            "url": "/configure/videos/video123",
            "play_url": "/?play=video123",
            "raw_url": "/configure/videos/video123/raw",
            "load_url": "/configure/videos/video123/load",
            "vote": "",
        }
    ]


def test_videos_overview_content_falls_back_to_unknown_channel() -> None:
    videos = [
        {
            "id": "video456",
            "title": None,
            "channel_title": None,
            "label": "whitelisted",
            "has_raw": False,
        }
    ]

    items = web._videos_overview_content(videos)  # type: ignore[attr-defined]

    assert items == [
        {
            "video_id": "video456",
            "channel_title": "Unknown channel",
            "video_title": "video456",
            "url": "/configure/videos/video456",
            "play_url": "/?play=video456",
            "raw_url": None,
            "load_url": "/configure/videos/video456/load",
            "vote": "👍",
        }
    ]
