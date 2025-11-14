"""Tests for helper functions supporting the now playing section."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("sqlmodel")

from mytube import web


def test_select_thumbnail_prefers_closest_width() -> None:
    raw_video = {
        "snippet": {
            "thumbnails": {
                "default": {"url": "http://example.com/120.jpg", "width": 120},
                "medium": {"url": "http://example.com/320.jpg", "width": 320},
                "high": {"url": "http://example.com/480.jpg", "width": 480},
            }
        }
    }

    url = web._select_thumbnail_url(raw_video, desired_width=320)  # type: ignore[attr-defined]

    assert url == "http://example.com/320.jpg"


def test_select_thumbnail_breaks_ties_with_larger_width() -> None:
    raw_video = {
        "snippet": {
            "thumbnails": {
                "small": {"url": "http://example.com/300.jpg", "width": 300},
                "large": {"url": "http://example.com/340.jpg", "width": 340},
            }
        }
    }

    url = web._select_thumbnail_url(raw_video, desired_width=320)  # type: ignore[attr-defined]

    assert url == "http://example.com/340.jpg"


def test_select_thumbnail_uses_fallback_when_width_missing() -> None:
    raw_video = {
        "snippet": {
            "thumbnails": {
                "mystery": {"url": "http://example.com/mystery.jpg"},
            }
        }
    }

    url = web._select_thumbnail_url(raw_video, desired_width=320)  # type: ignore[attr-defined]

    assert url == "http://example.com/mystery.jpg"


def test_build_playing_context_defaults_title_to_identifier() -> None:
    video_record = {
        "id": "abc123",
        "title": None,
        "raw_json": {
            "snippet": {
                "thumbnails": {
                    "medium": {
                        "url": "http://example.com/320.jpg",
                        "width": 320,
                    }
                }
            }
        },
    }

    context = web._build_playing_context(video_record, "abc123")  # type: ignore[attr-defined]

    assert context["title"] == "abc123"
    assert context["thumbnail_url"] == "http://example.com/320.jpg"
    assert context["video_id"] == "abc123"
