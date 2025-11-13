"""SQLite helpers for storing YouTube playlist and channel data."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

DB_PATH = Path.cwd() / "data" / "mytube.db"


def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    """Ensure the playlist, playlist item, channel, and resource tables exist."""

    with _get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist_items (
                id TEXT PRIMARY KEY,
                playlist_id TEXT NOT NULL,
                position INTEGER,
                title TEXT,
                description TEXT,
                published_at TEXT,
                raw_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_playlist_items_playlist_id
            ON playlist_items(playlist_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY,
                title TEXT,
                description TEXT,
                raw_json TEXT NOT NULL,
                retrieved_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_labels (
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                label TEXT NOT NULL CHECK(label IN ('whitelisted', 'blacklisted')),
                PRIMARY KEY (resource_type, resource_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                title TEXT,
                description TEXT,
                raw_json TEXT NOT NULL,
                retrieved_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                title TEXT,
                description TEXT,
                raw_json TEXT NOT NULL,
                retrieved_at TEXT NOT NULL
            )
            """
        )


def save_playlist_items(playlist_id: str, items: Iterable[dict]) -> None:
    """Replace stored playlist items with the provided dataset."""

    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not item_id:
            continue
        snippet = item.get("snippet") or {}
        snippet_playlist_id = snippet.get("playlistId") or playlist_id
        position = snippet.get("position")
        title = snippet.get("title")
        description = snippet.get("description")
        published_at = snippet.get("publishedAt")
        records.append(
            (
                item_id,
                snippet_playlist_id,
                position,
                title,
                description,
                published_at,
                json.dumps(item, separators=(",", ":")),
            )
        )

    with _get_connection() as connection:
        connection.execute(
            "DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,)
        )
        if records:
            connection.executemany(
                """
                INSERT OR REPLACE INTO playlist_items (
                    id,
                    playlist_id,
                    position,
                    title,
                    description,
                    published_at,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )


def fetch_playlist_items(playlist_id: str) -> list[dict]:
    """Return stored playlist items for the given playlist."""

    with _get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT raw_json
            FROM playlist_items
            WHERE playlist_id = ?
            ORDER BY position, title, id
            """,
            (playlist_id,),
        )
        rows = cursor.fetchall()
    return [json.loads(row["raw_json"]) for row in rows]


def save_playlist(playlist: dict, *, retrieved_at: datetime) -> None:
    """Insert or update a YouTube playlist record."""

    playlist_id = playlist.get("id")
    if not playlist_id:
        raise ValueError("Playlist data is missing an 'id'")

    snippet = playlist.get("snippet") or {}
    title = snippet.get("title")
    description = snippet.get("description")
    raw_json = json.dumps(playlist, separators=(",", ":"))
    with _get_connection() as connection:
        connection.execute(
            """
            INSERT INTO playlists (
                id,
                title,
                description,
                raw_json,
                retrieved_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                raw_json=excluded.raw_json,
                retrieved_at=excluded.retrieved_at
            """,
            (
                playlist_id,
                title,
                description,
                raw_json,
                retrieved_at.isoformat(),
            ),
        )


def fetch_playlist(playlist_id: str) -> Optional[dict]:
    """Fetch a stored YouTube playlist record."""

    with _get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT id, title, description, raw_json, retrieved_at
            FROM playlists
            WHERE id = ?
            """,
            (playlist_id,),
        )
        row = cursor.fetchone()
        label = (
            fetch_resource_label("playlist", playlist_id, connection=connection)
            if row
            else None
        )
    if not row:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "raw_json": json.loads(row["raw_json"]),
        "retrieved_at": row["retrieved_at"],
        "label": label,
        "whitelist": label == "whitelisted" if label is not None else False,
    }
def save_channel(channel: dict, *, retrieved_at: datetime) -> None:
    """Insert or update a YouTube channel record."""

    channel_id = channel.get("id")
    if not channel_id:
        raise ValueError("Channel data is missing an 'id'")

    snippet = channel.get("snippet") or {}
    title = snippet.get("title")
    description = snippet.get("description")
    raw_json = json.dumps(channel, separators=(",", ":"))
    with _get_connection() as connection:
        connection.execute(
            """
            INSERT INTO channels (
                id,
                title,
                description,
                raw_json,
                retrieved_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                raw_json=excluded.raw_json,
                retrieved_at=excluded.retrieved_at
            """,
            (
                channel_id,
                title,
                description,
                raw_json,
                retrieved_at.isoformat(),
            ),
        )


def save_video(video: dict, *, retrieved_at: datetime) -> None:
    """Insert or update a YouTube video record."""

    video_id = video.get("id")
    if not video_id:
        raise ValueError("Video data is missing an 'id'")

    snippet = video.get("snippet") or {}
    title = snippet.get("title")
    description = snippet.get("description")
    raw_json = json.dumps(video, separators=(",", ":"))
    with _get_connection() as connection:
        connection.execute(
            """
            INSERT INTO videos (
                id,
                title,
                description,
                raw_json,
                retrieved_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                raw_json=excluded.raw_json,
                retrieved_at=excluded.retrieved_at
            """,
            (
                video_id,
                title,
                description,
                raw_json,
                retrieved_at.isoformat(),
            ),
        )


def fetch_channel(channel_id: str) -> Optional[dict]:
    """Fetch a stored YouTube channel record."""

    with _get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT id, title, description, raw_json, retrieved_at
            FROM channels
            WHERE id = ?
            """,
            (channel_id,),
        )
        row = cursor.fetchone()
        label = (
            fetch_resource_label("channel", channel_id, connection=connection)
            if row
            else None
        )
    if not row:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "raw_json": json.loads(row["raw_json"]),
        "retrieved_at": row["retrieved_at"],
        "label": label,
        "whitelist": label == "whitelisted" if label is not None else False,
    }


def fetch_video(video_id: str) -> Optional[dict]:
    """Fetch a stored YouTube video record."""

    with _get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT id, title, description, raw_json, retrieved_at
            FROM videos
            WHERE id = ?
            """,
            (video_id,),
        )
        row = cursor.fetchone()
        label = (
            fetch_resource_label("video", video_id, connection=connection)
            if row
            else None
        )
    if not row:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "raw_json": json.loads(row["raw_json"]),
        "retrieved_at": row["retrieved_at"],
        "label": label,
        "whitelist": label == "whitelisted" if label is not None else False,
    }


def set_resource_label(
    resource_type: str, resource_id: str, label: str, *, connection: sqlite3.Connection | None = None
) -> None:
    """Persist a label for a resource."""

    if label not in {"whitelisted", "blacklisted"}:
        raise ValueError("Label must be 'whitelisted' or 'blacklisted'")

    def _execute(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO resource_labels (resource_type, resource_id, label)
            VALUES (?, ?, ?)
            ON CONFLICT(resource_type, resource_id) DO UPDATE SET
                label=excluded.label
            """,
            (resource_type, resource_id, label),
        )

    if connection is not None:
        _execute(connection)
        return

    with _get_connection() as conn:
        _execute(conn)


def fetch_resource_label(
    resource_type: str, resource_id: str, *, connection: sqlite3.Connection | None = None
) -> Optional[str]:
    """Retrieve the stored label for a resource, if any."""

    def _query(conn: sqlite3.Connection) -> Optional[str]:
        cursor = conn.execute(
            """
            SELECT label
            FROM resource_labels
            WHERE resource_type = ? AND resource_id = ?
            """,
            (resource_type, resource_id),
        )
        row = cursor.fetchone()
        return row["label"] if row else None

    if connection is not None:
        return _query(connection)

    with _get_connection() as conn:
        return _query(conn)


def fetch_all_channels() -> list[dict]:
    """Return stored channel records including their labels."""

    with _get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT c.id, c.title, c.retrieved_at, rl.label
            FROM channels AS c
            LEFT JOIN resource_labels AS rl
                ON rl.resource_type = 'channel' AND rl.resource_id = c.id
            ORDER BY (rl.label IS NULL), datetime(c.retrieved_at) DESC, c.id
            """
        )
        rows = cursor.fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "retrieved_at": row["retrieved_at"],
            "label": row["label"],
        }
        for row in rows
    ]


def fetch_all_videos() -> list[dict]:
    """Return stored video records including their labels."""

    with _get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT v.id, v.title, v.retrieved_at, rl.label
            FROM videos AS v
            LEFT JOIN resource_labels AS rl
                ON rl.resource_type = 'video' AND rl.resource_id = v.id
            ORDER BY (rl.label IS NULL), datetime(v.retrieved_at) DESC, v.id
            """
        )
        rows = cursor.fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "retrieved_at": row["retrieved_at"],
            "label": row["label"],
        }
        for row in rows
    ]


__all__ = [
    "initialize_database",
    "save_playlist_items",
    "fetch_playlist_items",
    "save_playlist",
    "fetch_playlist",
    "save_channel",
    "save_video",
    "fetch_channel",
    "fetch_video",
    "fetch_all_channels",
    "fetch_all_videos",
    "set_resource_label",
    "fetch_resource_label",
]

