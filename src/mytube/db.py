"""SQLite helpers for storing YouTube playlist item data."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

DB_PATH = Path.cwd() / "data" / "mytube.db"


def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    """Ensure the playlist items table exists."""

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


__all__ = [
    "initialize_database",
    "save_playlist_items",
    "fetch_playlist_items",
]

