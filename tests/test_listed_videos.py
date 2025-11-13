from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("sqlmodel")

from mytube import db, web


@pytest.fixture(autouse=True)
def isolate_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mytube.db")
    db._engine = None
    db.initialize_database()
    yield
    db._engine = None


def _make_playlist_item(video_id: str, item_id: str = "item") -> dict:
    return {
        "id": f"{item_id}-{video_id}",
        "snippet": {
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
            "playlistId": "UUuploads",
            "title": f"Video {video_id}",
            "description": "",
            "position": 0,
        },
        "contentDetails": {"videoId": video_id},
    }


def test_channel_label_populates_channel_identifier():
    retrieved_at = datetime.now(timezone.utc)
    channel_payload = {
        "id": "UC123",
        "snippet": {"title": "Sample Channel"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UUuploads"}},
    }
    db.save_channel(channel_payload, retrieved_at=retrieved_at)
    db.save_playlist_items("UUuploads", [_make_playlist_item("video123")])
    db.set_resource_label("channel", "UC123", "whitelisted")

    db.repopulate_listed_videos()
    listing = db.fetch_listed_video("video123")

    assert listing is not None
    assert "UC123" in listing["whitelisted_by"]


def test_listed_videos_content_uses_reference_links():
    retrieved_at = datetime.now(timezone.utc)
    channel_payload = {
        "id": "UC123",
        "snippet": {"title": "Sample Channel"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UUuploads"}},
    }
    db.save_channel(channel_payload, retrieved_at=retrieved_at)
    playlist_payload = {
        "id": "PL456",
        "snippet": {"title": "Sample Playlist"},
    }
    db.save_playlist(playlist_payload, retrieved_at=retrieved_at)

    videos = [
        {
            "video_id": "video123",
            "title": "Test Video",
            "whitelisted_by": ["UC123"],
            "blacklisted_by": ["PL456"],
        }
    ]

    html = web._listed_videos_content("whitelist", videos, "/regen")

    assert "<p>👍" in html
    assert "/configure/channels/UC123" in html
    assert "<p>👎" in html
    assert "/configure/playlists/PL456" in html
