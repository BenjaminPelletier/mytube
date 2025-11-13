"""SQLite helpers for storing YouTube playlist and channel data."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import CheckConstraint, and_, delete
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

DB_PATH = Path.cwd() / "data" / "mytube.db"

_engine: Engine | None = None


def _get_engine() -> Engine:
    """Create (or reuse) the SQLite engine."""

    global _engine
    if _engine is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{DB_PATH}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine


class PlaylistItem(SQLModel, table=True):
    """SQLModel representation of a playlist item."""

    __tablename__ = "playlist_items"

    id: str = Field(primary_key=True)
    playlist_id: str = Field(index=True)
    position: int | None = None
    title: str | None = None
    description: str | None = None
    published_at: str | None = None
    raw_json: str = Field(nullable=False)


class Playlist(SQLModel, table=True):
    """SQLModel representation of a playlist."""

    __tablename__ = "playlists"

    id: str = Field(primary_key=True)
    title: str | None = None
    description: str | None = None
    raw_json: str = Field(nullable=False)
    retrieved_at: str = Field(nullable=False)


class Channel(SQLModel, table=True):
    """SQLModel representation of a channel."""

    __tablename__ = "channels"

    id: str = Field(primary_key=True)
    title: str | None = None
    description: str | None = None
    raw_json: str = Field(nullable=False)
    retrieved_at: str = Field(nullable=False)


class ChannelSection(SQLModel, table=True):
    """SQLModel representation of a channel section."""

    __tablename__ = "channel_sections"

    id: str = Field(primary_key=True)
    channel_id: str = Field(index=True)
    title: str | None = None
    raw_json: str = Field(nullable=False)
    retrieved_at: str = Field(nullable=False)


class Video(SQLModel, table=True):
    """SQLModel representation of a video."""

    __tablename__ = "videos"

    id: str = Field(primary_key=True)
    title: str | None = None
    description: str | None = None
    raw_json: str = Field(nullable=False)
    retrieved_at: str = Field(nullable=False)


class ResourceLabel(SQLModel, table=True):
    """SQLModel representation of a resource label."""

    __tablename__ = "resource_labels"
    __table_args__ = (
        CheckConstraint(
            "label IN ('whitelisted', 'blacklisted')",
            name="ck_resource_labels_label",
        ),
    )

    resource_type: str = Field(primary_key=True)
    resource_id: str = Field(primary_key=True)
    label: str = Field(nullable=False)


def initialize_database() -> None:
    """Ensure the playlist, playlist item, channel, and resource tables exist."""

    engine = _get_engine()
    SQLModel.metadata.create_all(engine)


def save_playlist_items(playlist_id: str, items: Iterable[dict]) -> None:
    """Replace stored playlist items with the provided dataset."""

    records: list[PlaylistItem] = []
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
            PlaylistItem(
                id=item_id,
                playlist_id=snippet_playlist_id,
                position=position,
                title=title,
                description=description,
                published_at=published_at,
                raw_json=json.dumps(item, separators=(",", ":")),
            )
        )

    engine = _get_engine()
    with Session(engine) as session:
        session.exec(
            delete(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id)
        )
        if records:
            session.add_all(records)
        session.commit()


def fetch_playlist_items(playlist_id: str) -> list[dict]:
    """Return stored playlist items for the given playlist."""

    engine = _get_engine()
    with Session(engine) as session:
        statement = (
            select(PlaylistItem)
            .where(PlaylistItem.playlist_id == playlist_id)
            .order_by(PlaylistItem.position, PlaylistItem.title, PlaylistItem.id)
        )
        rows = session.exec(statement).all()
    return [json.loads(row.raw_json) for row in rows]


def save_playlist(playlist: dict, *, retrieved_at: datetime) -> None:
    """Insert or update a YouTube playlist record."""

    playlist_id = playlist.get("id")
    if not playlist_id:
        raise ValueError("Playlist data is missing an 'id'")

    snippet = playlist.get("snippet") or {}
    title = snippet.get("title")
    description = snippet.get("description")
    raw_json = json.dumps(playlist, separators=(",", ":"))
    engine = _get_engine()
    with Session(engine) as session:
        existing = session.get(Playlist, playlist_id)
        if existing:
            existing.title = title
            existing.description = description
            existing.raw_json = raw_json
            existing.retrieved_at = retrieved_at.isoformat()
        else:
            session.add(
                Playlist(
                    id=playlist_id,
                    title=title,
                    description=description,
                    raw_json=raw_json,
                    retrieved_at=retrieved_at.isoformat(),
                )
            )
        session.commit()


def fetch_playlist(playlist_id: str) -> dict | None:
    """Fetch a stored YouTube playlist record."""

    engine = _get_engine()
    with Session(engine) as session:
        record = session.get(Playlist, playlist_id)
        label = (
            fetch_resource_label("playlist", playlist_id, session=session)
            if record
            else None
        )
    if not record:
        return None
    return {
        "id": record.id,
        "title": record.title,
        "description": record.description,
        "raw_json": json.loads(record.raw_json),
        "retrieved_at": record.retrieved_at,
        "label": label,
        "whitelist": label == "whitelisted" if label is not None else False,
    }


def fetch_all_playlists() -> list[dict]:
    """Return stored playlist records including their labels."""

    engine = _get_engine()
    with Session(engine) as session:
        statement = (
            select(
                Playlist.id,
                Playlist.title,
                Playlist.raw_json,
                Playlist.retrieved_at,
                ResourceLabel.label,
            )
            .select_from(Playlist)
            .join(
                ResourceLabel,
                and_(
                    ResourceLabel.resource_type == "playlist",
                    ResourceLabel.resource_id == Playlist.id,
                ),
                isouter=True,
            )
            .order_by(
                ResourceLabel.label.is_(None),
                Playlist.retrieved_at.desc(),
                Playlist.id,
            )
        )
        rows = session.exec(statement).all()

    results: list[dict] = []
    for playlist_id, title, raw_json, retrieved_at, label in rows:
        channel_id: str | None = None
        channel_title: str | None = None
        if raw_json:
            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                payload = {}
            snippet = payload.get("snippet") if isinstance(payload, dict) else {}
            if isinstance(snippet, dict):
                channel_id = snippet.get("channelId")
                channel_title = snippet.get("channelTitle")
        results.append(
            {
                "id": playlist_id,
                "title": title,
                "retrieved_at": retrieved_at,
                "label": label,
                "channel_id": channel_id,
                "channel_title": channel_title,
            }
        )

    return results


def save_channel(channel: dict, *, retrieved_at: datetime) -> None:
    """Insert or update a YouTube channel record."""

    channel_id = channel.get("id")
    if not channel_id:
        raise ValueError("Channel data is missing an 'id'")

    snippet = channel.get("snippet") or {}
    title = snippet.get("title")
    description = snippet.get("description")
    raw_json = json.dumps(channel, separators=(",", ":"))
    engine = _get_engine()
    with Session(engine) as session:
        existing = session.get(Channel, channel_id)
        if existing:
            existing.title = title
            existing.description = description
            existing.raw_json = raw_json
            existing.retrieved_at = retrieved_at.isoformat()
        else:
            session.add(
                Channel(
                    id=channel_id,
                    title=title,
                    description=description,
                    raw_json=raw_json,
                    retrieved_at=retrieved_at.isoformat(),
                )
            )
        session.commit()


def save_channel_sections(
    channel_id: str, sections: Iterable[dict], *, retrieved_at: datetime
) -> None:
    """Insert or update YouTube channel section records."""

    if not channel_id:
        raise ValueError("Channel sections require a channel identifier")

    engine = _get_engine()
    with Session(engine) as session:
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_id = section.get("id")
            if not section_id:
                continue
            snippet = section.get("snippet") or {}
            title = snippet.get("title")
            raw_json = json.dumps(section, separators=(",", ":"))
            existing = session.get(ChannelSection, section_id)
            if existing:
                existing.channel_id = channel_id
                existing.title = title
                existing.raw_json = raw_json
                existing.retrieved_at = retrieved_at.isoformat()
            else:
                session.add(
                    ChannelSection(
                        id=section_id,
                        channel_id=channel_id,
                        title=title,
                        raw_json=raw_json,
                        retrieved_at=retrieved_at.isoformat(),
                    )
                )
        session.commit()


def save_video(video: dict, *, retrieved_at: datetime) -> None:
    """Insert or update a YouTube video record."""

    video_id = video.get("id")
    if not video_id:
        raise ValueError("Video data is missing an 'id'")

    snippet = video.get("snippet") or {}
    title = snippet.get("title")
    description = snippet.get("description")
    raw_json = json.dumps(video, separators=(",", ":"))
    engine = _get_engine()
    with Session(engine) as session:
        existing = session.get(Video, video_id)
        if existing:
            existing.title = title
            existing.description = description
            existing.raw_json = raw_json
            existing.retrieved_at = retrieved_at.isoformat()
        else:
            session.add(
                Video(
                    id=video_id,
                    title=title,
                    description=description,
                    raw_json=raw_json,
                    retrieved_at=retrieved_at.isoformat(),
                )
            )
        session.commit()


def fetch_channel(channel_id: str) -> dict | None:
    """Fetch a stored YouTube channel record."""

    engine = _get_engine()
    with Session(engine) as session:
        record = session.get(Channel, channel_id)
        label = (
            fetch_resource_label("channel", channel_id, session=session)
            if record
            else None
        )
    if not record:
        return None
    return {
        "id": record.id,
        "title": record.title,
        "description": record.description,
        "raw_json": json.loads(record.raw_json),
        "retrieved_at": record.retrieved_at,
        "label": label,
        "whitelist": label == "whitelisted" if label is not None else False,
    }


def fetch_channel_sections(channel_id: str) -> list[dict]:
    """Fetch stored channel section records for a channel."""

    engine = _get_engine()
    with Session(engine) as session:
        statement = (
            select(ChannelSection)
            .where(ChannelSection.channel_id == channel_id)
            .order_by(ChannelSection.title.is_(None), ChannelSection.title, ChannelSection.id)
        )
        rows = session.exec(statement).all()
    return [
        {
            "id": row.id,
            "channel_id": row.channel_id,
            "title": row.title,
            "raw_json": json.loads(row.raw_json),
            "retrieved_at": row.retrieved_at,
        }
        for row in rows
    ]


def fetch_video(video_id: str) -> dict | None:
    """Fetch a stored YouTube video record."""

    engine = _get_engine()
    with Session(engine) as session:
        record = session.get(Video, video_id)
        label = (
            fetch_resource_label("video", video_id, session=session)
            if record
            else None
        )
    if not record:
        return None
    return {
        "id": record.id,
        "title": record.title,
        "description": record.description,
        "raw_json": json.loads(record.raw_json),
        "retrieved_at": record.retrieved_at,
        "label": label,
        "whitelist": label == "whitelisted" if label is not None else False,
    }


def set_resource_label(
    resource_type: str,
    resource_id: str,
    label: str,
    *,
    session: Session | None = None,
) -> None:
    """Persist a label for a resource."""

    if label not in {"whitelisted", "blacklisted"}:
        raise ValueError("Label must be 'whitelisted' or 'blacklisted'")

    def _persist(db_session: Session) -> None:
        existing = db_session.get(ResourceLabel, (resource_type, resource_id))
        if existing:
            existing.label = label
        else:
            db_session.add(
                ResourceLabel(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    label=label,
                )
            )

    if session is not None:
        _persist(session)
        return

    engine = _get_engine()
    with Session(engine) as session_obj:
        _persist(session_obj)
        session_obj.commit()


def fetch_resource_label(
    resource_type: str,
    resource_id: str,
    *,
    session: Session | None = None,
) -> str | None:
    """Retrieve the stored label for a resource, if any."""

    def _query(db_session: Session) -> str | None:
        record = db_session.get(ResourceLabel, (resource_type, resource_id))
        return record.label if record else None

    if session is not None:
        return _query(session)

    engine = _get_engine()
    with Session(engine) as session_obj:
        return _query(session_obj)


def fetch_all_channels() -> list[dict]:
    """Return stored channel records including their labels."""

    engine = _get_engine()
    with Session(engine) as session:
        statement = (
            select(Channel.id, Channel.title, Channel.retrieved_at, ResourceLabel.label)
            .select_from(Channel)
            .join(
                ResourceLabel,
                and_(
                    ResourceLabel.resource_type == "channel",
                    ResourceLabel.resource_id == Channel.id,
                ),
                isouter=True,
            )
            .order_by(
                ResourceLabel.label.is_(None),
                Channel.retrieved_at.desc(),
                Channel.id,
            )
        )
        rows = session.exec(statement).all()
    return [
        {
            "id": row[0],
            "title": row[1],
            "retrieved_at": row[2],
            "label": row[3],
        }
        for row in rows
    ]


def fetch_all_videos() -> list[dict]:
    """Return stored video records including their labels."""

    engine = _get_engine()
    with Session(engine) as session:
        statement = (
            select(Video.id, Video.title, Video.retrieved_at, ResourceLabel.label)
            .select_from(Video)
            .join(
                ResourceLabel,
                and_(
                    ResourceLabel.resource_type == "video",
                    ResourceLabel.resource_id == Video.id,
                ),
                isouter=True,
            )
            .order_by(
                ResourceLabel.label.is_(None),
                Video.retrieved_at.desc(),
                Video.id,
            )
        )
        rows = session.exec(statement).all()
    return [
        {
            "id": row[0],
            "title": row[1],
            "retrieved_at": row[2],
            "label": row[3],
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
    "save_channel_sections",
    "save_video",
    "fetch_channel",
    "fetch_channel_sections",
    "fetch_video",
    "fetch_all_playlists",
    "fetch_all_channels",
    "fetch_all_videos",
    "set_resource_label",
    "fetch_resource_label",
]

