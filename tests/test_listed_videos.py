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


def _make_playlist_item(
    video_id: str,
    item_id: str = "item",
    *,
    title: str | None = None,
    description: str | None = "",
) -> dict:
    return {
        "id": f"{item_id}-{video_id}",
        "snippet": {
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
            "playlistId": "UUuploads",
            "title": title if title is not None else f"Video {video_id}",
            "description": description,
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
    db.save_playlist_items(
        "UUuploads",
        [_make_playlist_item("video123")],
        retrieved_at=retrieved_at,
    )
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

    context = web._listed_videos_content("whitelist", videos, "/regen")

    class DummyRequest:
        def url_for(self, name: str, **params: str) -> str:  # pragma: no cover - trivial
            path = params.get("path")
            if name == "static" and path:
                return f"/static/{path}"
            if params:
                return "/" + "/".join([name, *params.values()])
            return f"/{name}"

    template = web.templates.env.get_template("configure/listed_videos.html")
    html = template.render(
        {
            **context,
            "request": DummyRequest(),
            "heading": "",
            "navigation": [],
            "form_action": "",
            "resource_value": "",
            "show_resource_form": False,
        }
    )

    assert 'thumb-emoji thumb-up">👍' in html
    assert "/configure/channels/UC123" in html
    assert 'thumb-emoji thumb-down">👎' in html
    assert "/configure/playlists/PL456" in html


def test_playlist_items_create_partial_video_entries():
    retrieved_at = datetime.now(timezone.utc)
    item = _make_playlist_item("video999", title="Stored Title", description="Partial")

    db.save_playlist_items(
        "UUuploads",
        [item],
        retrieved_at=retrieved_at,
    )

    video = db.fetch_video("video999")

    assert video is not None
    assert video["title"] == "Stored Title"
    assert video["description"] == "Partial"
    assert video["retrieved_at"] == retrieved_at.isoformat()
    assert video["raw_json"] is None


def test_playlist_items_do_not_overwrite_full_video_data():
    initial_retrieved_at = datetime.now(timezone.utc)
    db.save_playlist_items(
        "UUuploads",
        [_make_playlist_item("video321", title="Initial Title")],
        retrieved_at=initial_retrieved_at,
    )

    full_video_retrieved_at = datetime.now(timezone.utc)
    db.save_video(
        {
            "id": "video321",
            "snippet": {
                "title": "Full Title",
                "description": "Full description",
            },
        },
        retrieved_at=full_video_retrieved_at,
    )

    updated_item = _make_playlist_item("video321", title="Playlist Title", description="New")
    later_retrieved_at = datetime.now(timezone.utc)
    db.save_playlist_items(
        "UUuploads",
        [updated_item],
        retrieved_at=later_retrieved_at,
    )

    video = db.fetch_video("video321")

    assert video is not None
    assert video["title"] == "Full Title"
    assert video["description"] == "Full description"
    assert video["retrieved_at"] == full_video_retrieved_at.isoformat()
    assert video["raw_json"] is not None
