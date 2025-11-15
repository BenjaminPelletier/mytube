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
        }
    ]

    items = web._videos_overview_content(videos)  # type: ignore[attr-defined]

    assert items == [
        {
            "title": "[Sample Channel] Sample Video",
            "url": "/configure/videos/video123",
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
        }
    ]

    items = web._videos_overview_content(videos)  # type: ignore[attr-defined]

    assert items == [
        {
            "title": "[Unknown channel] video456",
            "url": "/configure/videos/video456",
            "vote": "👍",
        }
    ]
