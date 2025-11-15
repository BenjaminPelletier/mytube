"""SQLite helpers for storing YouTube playlist and channel data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import CheckConstraint, Column, Text, and_, delete, or_
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
    uploads_playlist: str | None = Field(default=None, index=True)


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
    channel_title: str | None = None


class ResourceLabel(SQLModel, table=True):
    """SQLModel representation of a resource label."""

    __tablename__ = "resource_labels"
    __table_args__ = (
        CheckConstraint(
            "label IN ('whitelisted', 'blacklisted', 'favorite', 'flagged')",
            name="ck_resource_labels_label",
        ),
    )

    resource_type: str = Field(primary_key=True)
    resource_id: str = Field(primary_key=True)
    label: str = Field(primary_key=True)


class ListedVideo(SQLModel, table=True):
    """SQLModel representation of a whitelisted/blacklisted video."""

    __tablename__ = "listed_videos"

    video_id: str = Field(primary_key=True)
    whitelisted_by: str | None = Field(
        default=None,
        sa_column=Column("whitelisted_by", Text, nullable=True),
    )
    blacklisted_by: str | None = Field(
        default=None,
        sa_column=Column("blacklisted_by", Text, nullable=True),
    )
    disqualifying_attributes: str | None = Field(
        default=None,
        sa_column=Column("disqualifying_attributes", Text, nullable=True),
    )


class Setting(SQLModel, table=True):
    """Application-level key/value setting."""

    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: str = Field(sa_column=Column("value", Text, nullable=False))


class HistoryEvent(SQLModel, table=True):
    """Record of application viewing activity events."""

    __tablename__ = "history_events"

    id: int | None = Field(default=None, primary_key=True)
    event_type: str = Field(nullable=False)
    created_at: str = Field(nullable=False, index=True)
    metadata_json: str = Field(sa_column=Column("metadata_json", Text, nullable=False))


def initialize_database() -> None:
    """Ensure the playlist, playlist item, channel, and resource tables exist."""

    engine = _get_engine()
    SQLModel.metadata.create_all(engine)


def save_playlist_items(
    playlist_id: str, items: Iterable[dict], *, retrieved_at: datetime | None = None
) -> None:
    """Replace stored playlist items with the provided dataset."""

    records: list[PlaylistItem] = []
    video_snippets: dict[str, tuple[str | None, str | None]] = {}
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
        raw_json = json.dumps(item, separators=(",", ":"))
        records.append(
            PlaylistItem(
                id=item_id,
                playlist_id=snippet_playlist_id,
                position=position,
                title=title,
                description=description,
                published_at=published_at,
                raw_json=raw_json,
            )
        )
        video_id = _extract_video_id_from_playlist_item(raw_json)
        if video_id:
            video_snippets[video_id] = (title, description)

    engine = _get_engine()
    retrieved_value = (retrieved_at or datetime.now(timezone.utc)).isoformat()
    with Session(engine) as session:
        session.exec(
            delete(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id)
        )
        if records:
            session.add_all(records)
        if video_snippets:
            for video_id, (video_title, video_description) in video_snippets.items():
                existing_video = session.get(Video, video_id)
                if existing_video:
                    if existing_video.raw_json:
                        continue
                    existing_video.title = video_title
                    existing_video.description = video_description
                    existing_video.retrieved_at = retrieved_value
                    if not existing_video.raw_json:
                        existing_video.raw_json = ""
                else:
                    session.add(
                        Video(
                            id=video_id,
                            title=video_title,
                            description=video_description,
                            raw_json="",
                            retrieved_at=retrieved_value,
                        )
                    )
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
            fetch_resource_label(
                "playlist",
                playlist_id,
                labels={"whitelisted", "blacklisted"},
                session=session,
            )
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
                    ResourceLabel.label.in_({"whitelisted", "blacklisted"}),
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
    uploads_playlist: str | None = None
    content_details = channel.get("contentDetails")
    if isinstance(content_details, dict):
        related_playlists = content_details.get("relatedPlaylists")
        if isinstance(related_playlists, dict):
            uploads_value = related_playlists.get("uploads")
            if isinstance(uploads_value, str) and uploads_value:
                uploads_playlist = uploads_value
    engine = _get_engine()
    with Session(engine) as session:
        existing = session.get(Channel, channel_id)
        if existing:
            existing.title = title
            existing.description = description
            existing.raw_json = raw_json
            existing.retrieved_at = retrieved_at.isoformat()
            existing.uploads_playlist = uploads_playlist
        else:
            session.add(
                Channel(
                    id=channel_id,
                    title=title,
                    description=description,
                    raw_json=raw_json,
                    retrieved_at=retrieved_at.isoformat(),
                    uploads_playlist=uploads_playlist,
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
    channel_title_value = snippet.get("channelTitle")
    channel_title = (
        channel_title_value.strip()
        if isinstance(channel_title_value, str) and channel_title_value.strip()
        else None
    )
    raw_json = json.dumps(video, separators=(",", ":"))
    engine = _get_engine()
    with Session(engine) as session:
        existing = session.get(Video, video_id)
        if existing:
            existing.title = title
            existing.description = description
            existing.raw_json = raw_json
            existing.retrieved_at = retrieved_at.isoformat()
            if channel_title:
                existing.channel_title = channel_title
        else:
            session.add(
                Video(
                    id=video_id,
                    title=title,
                    description=description,
                    raw_json=raw_json,
                    retrieved_at=retrieved_at.isoformat(),
                    channel_title=channel_title,
                )
            )
        session.commit()


def fetch_video_ids_missing_raw_json(
    limit: int, *, exclude: Iterable[str] | None = None
) -> list[str]:
    """Return video IDs that are missing raw JSON payloads."""

    if limit <= 0:
        return []

    exclude_set = {str(video_id) for video_id in (exclude or []) if str(video_id)}

    engine = _get_engine()
    with Session(engine) as session:
        statement_limit = limit + len(exclude_set) if exclude_set else limit
        statement = (
            select(Video.id)
            .where(or_(Video.raw_json == "", Video.raw_json.is_(None)))
            .order_by(Video.retrieved_at.desc(), Video.id)
            .limit(statement_limit)
        )
        rows = session.exec(statement).all()

    missing_ids: list[str] = []
    for row in rows:
        if not isinstance(row, str):
            continue
        if row in exclude_set:
            continue
        missing_ids.append(row)
        if len(missing_ids) >= limit:
            break
    return missing_ids


def fetch_channel(channel_id: str) -> dict | None:
    """Fetch a stored YouTube channel record."""

    engine = _get_engine()
    with Session(engine) as session:
        record = session.get(Channel, channel_id)
        label = (
            fetch_resource_label(
                "channel",
                channel_id,
                labels={"whitelisted", "blacklisted"},
                session=session,
            )
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
        "uploads_playlist": record.uploads_playlist,
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
            fetch_resource_label(
                "video",
                video_id,
                labels={"whitelisted", "blacklisted"},
                session=session,
            )
            if record
            else None
        )
    if not record:
        return None
    raw_payload: Any | None = None
    if record.raw_json:
        try:
            raw_payload = json.loads(record.raw_json)
        except json.JSONDecodeError:
            raw_payload = None
    return {
        "id": record.id,
        "title": record.title,
        "description": record.description,
        "raw_json": raw_payload,
        "retrieved_at": record.retrieved_at,
        "label": label,
        "whitelist": label == "whitelisted" if label is not None else False,
        "channel_title": record.channel_title,
    }


def set_resource_label(
    resource_type: str,
    resource_id: str,
    label: str,
    *,
    session: Session | None = None,
) -> None:
    """Persist a label for a resource."""

    if label not in {"whitelisted", "blacklisted", "favorite", "flagged"}:
        raise ValueError(
            "Label must be one of 'whitelisted', 'blacklisted', 'favorite', or 'flagged'"
        )

    def _persist(db_session: Session) -> None:
        if label in {"whitelisted", "blacklisted"}:
            opposite = "blacklisted" if label == "whitelisted" else "whitelisted"
            db_session.exec(
                delete(ResourceLabel).where(
                    ResourceLabel.resource_type == resource_type,
                    ResourceLabel.resource_id == resource_id,
                    ResourceLabel.label == opposite,
                )
            )

        existing = db_session.get(ResourceLabel, (resource_type, resource_id, label))
        if not existing:
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


def clear_resource_label(
    resource_type: str,
    resource_id: str,
    label: str | None = None,
    *,
    session: Session | None = None,
) -> None:
    """Remove a stored label for a resource if it exists."""

    def _delete(db_session: Session) -> None:
        statement = (
            delete(ResourceLabel)
            .where(ResourceLabel.resource_type == resource_type)
            .where(ResourceLabel.resource_id == resource_id)
        )
        if label:
            statement = statement.where(ResourceLabel.label == label)
        db_session.exec(statement)

    if session is not None:
        _delete(session)
        return

    engine = _get_engine()
    with Session(engine) as session_obj:
        _delete(session_obj)
        session_obj.commit()


def fetch_resource_labels_map(
    resource_type: str,
    resource_ids: Iterable[str],
    labels: Iterable[str] | None = None,
    *,
    session: Session | None = None,
) -> dict[str, str]:
    """Return labels for the provided resource identifiers."""

    normalized_ids = [
        resource_id
        for resource_id in {str(value).strip() for value in resource_ids}
        if resource_id
    ]
    if not normalized_ids:
        return {}

    label_set = {label for label in labels or [] if isinstance(label, str)}
    label_filter = {label.strip() for label in label_set if label.strip()}

    def _query(db_session: Session) -> dict[str, str]:
        statement = (
            select(ResourceLabel.resource_id, ResourceLabel.label)
            .where(ResourceLabel.resource_type == resource_type)
            .where(ResourceLabel.resource_id.in_(normalized_ids))
        )
        if label_filter:
            statement = statement.where(ResourceLabel.label.in_(label_filter))
        return {resource_id: label for resource_id, label in db_session.exec(statement)}

    if session is not None:
        return _query(session)

    engine = _get_engine()
    with Session(engine) as session_obj:
        return _query(session_obj)


def fetch_settings(keys: Iterable[str] | None = None) -> dict[str, str]:
    """Retrieve stored application settings as a mapping of key to value."""

    engine = _get_engine()
    with Session(engine) as session:
        statement = select(Setting)
        key_list: list[str] | None = None
        if keys is not None:
            key_list = []
            for key in keys:
                if not isinstance(key, str):
                    key = str(key)
                normalized_key = key.strip()
                if normalized_key:
                    key_list.append(normalized_key)
            if not key_list:
                return {}
            statement = statement.where(Setting.key.in_(key_list))

        results = session.exec(statement)
        return {setting.key: setting.value for setting in results}


def store_settings(settings: dict[str, str | None]) -> None:
    """Persist the provided settings to the database."""

    if not settings:
        return

    engine = _get_engine()
    with Session(engine) as session:
        for key, value in settings.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue

            value_str = ""
            if value is not None:
                value_str = str(value).strip()

            existing = session.get(Setting, normalized_key)
            if not value_str:
                if existing is not None:
                    session.delete(existing)
                continue

            if existing is not None:
                existing.value = value_str
            else:
                session.add(Setting(key=normalized_key, value=value_str))

        session.commit()


def fetch_resource_label(
    resource_type: str,
    resource_id: str,
    labels: Iterable[str] | None = None,
    *,
    session: Session | None = None,
) -> str | None:
    """Retrieve the stored label for a resource, if any."""

    label_set = {label for label in labels or [] if isinstance(label, str)}
    label_filter = {label.strip() for label in label_set if label.strip()}

    def _query(db_session: Session) -> str | None:
        statement = select(ResourceLabel.label).where(
            ResourceLabel.resource_type == resource_type,
            ResourceLabel.resource_id == resource_id,
        )
        if label_filter:
            statement = statement.where(ResourceLabel.label.in_(label_filter))
        result = db_session.exec(statement).first()
        return result[0] if result else None

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
            select(
                Channel.id,
                Channel.title,
                Channel.retrieved_at,
                ResourceLabel.label,
            )
            .select_from(Channel)
            .join(
                ResourceLabel,
                and_(
                    ResourceLabel.resource_type == "channel",
                    ResourceLabel.resource_id == Channel.id,
                    ResourceLabel.label.in_({"whitelisted", "blacklisted"}),
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


def _load_identifier_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, str) and item]


def _dump_identifier_list(values: Iterable[str]) -> str | None:
    unique_values = sorted({value for value in values if isinstance(value, str) and value})
    if not unique_values:
        return None
    return json.dumps(unique_values, separators=(",", ":"))


def _extract_video_id_from_playlist_item(raw_json: str) -> str | None:
    try:
        payload: Any = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    snippet = payload.get("snippet")
    if isinstance(snippet, dict):
        resource = snippet.get("resourceId")
        if isinstance(resource, dict):
            video_id = resource.get("videoId")
            if isinstance(video_id, str) and video_id:
                return video_id
        video_id = snippet.get("videoId")
        if isinstance(video_id, str) and video_id:
            return video_id
    content_details = payload.get("contentDetails")
    if isinstance(content_details, dict):
        video_id = content_details.get("videoId")
        if isinstance(video_id, str) and video_id:
            return video_id
    return None


def fetch_listed_videos(list_type: str) -> list[dict[str, Any]]:
    """Return listed videos filtered by list type."""

    if list_type not in {"whitelist", "blacklist"}:
        raise ValueError("list_type must be 'whitelist' or 'blacklist'")

    engine = _get_engine()
    with Session(engine) as session:
        target_column = (
            ListedVideo.whitelisted_by
            if list_type == "whitelist"
            else ListedVideo.blacklisted_by
        )
        statement = (
            select(
                ListedVideo.video_id,
                ListedVideo.whitelisted_by,
                ListedVideo.blacklisted_by,
                ListedVideo.disqualifying_attributes,
                Video.title,
            )
            .select_from(ListedVideo)
            .join(Video, Video.id == ListedVideo.video_id, isouter=True)
            .where(target_column.is_not(None))
            .order_by(Video.title.is_(None), Video.title, ListedVideo.video_id)
        )
        rows = session.exec(statement).all()

    results: list[dict[str, Any]] = []
    for video_id, whitelisted_by, blacklisted_by, disqualifying_attributes, title in rows:
        results.append(
            {
                "video_id": video_id,
                "title": title,
                "whitelisted_by": _load_identifier_list(whitelisted_by),
                "blacklisted_by": _load_identifier_list(blacklisted_by),
                "disqualifying_attributes": _load_identifier_list(
                    disqualifying_attributes
                ),
            }
        )
    return results


def fetch_listed_video(video_id: str) -> dict[str, Any] | None:
    """Return the listed video entry for a specific video."""

    engine = _get_engine()
    with Session(engine) as session:
        record = session.get(ListedVideo, video_id)

    if not record:
        return None

    return {
        "video_id": record.video_id,
        "whitelisted_by": _load_identifier_list(record.whitelisted_by),
        "blacklisted_by": _load_identifier_list(record.blacklisted_by),
        "disqualifying_attributes": _load_identifier_list(
            record.disqualifying_attributes
        ),
    }


def refresh_listed_video_disqualifications(video_id: str) -> None:
    """Recalculate disqualifying attributes for a listed video."""

    normalized_id = video_id.strip()
    if not normalized_id:
        return

    engine = _get_engine()
    with Session(engine) as session:
        record = session.get(ListedVideo, normalized_id)
        if record is None:
            return

        attributes = set(
            _load_identifier_list(record.disqualifying_attributes)
        )
        flagged_label = fetch_resource_label(
            "video", normalized_id, labels={"flagged"}, session=session
        )
        if flagged_label == "flagged":
            attributes.add("flagged")
        else:
            attributes.discard("flagged")

        record.disqualifying_attributes = _dump_identifier_list(attributes)
        session.add(record)
        session.commit()


def repopulate_listed_videos() -> None:
    """Rebuild the ListedVideo table from stored labels."""

    engine = _get_engine()
    with Session(engine) as session:
        listings: dict[str, dict[str, set[str]]] = {}

        def _ensure_entry(video_id: str) -> dict[str, set[str]]:
            entry = listings.get(video_id)
            if entry is None:
                entry = {
                    "whitelisted_by": set(),
                    "blacklisted_by": set(),
                    "disqualifying_attributes": set(),
                }
                listings[video_id] = entry
            return entry

        # Step 1: labeled videos
        video_stmt = (
            select(ResourceLabel.resource_id, ResourceLabel.label)
            .where(ResourceLabel.resource_type == "video")
            .where(ResourceLabel.label.in_({"whitelisted", "blacklisted"}))
        )
        for video_id, label in session.exec(video_stmt):
            if not video_id:
                continue
            entry = _ensure_entry(video_id)
            key = "whitelisted_by" if label == "whitelisted" else "blacklisted_by"
            entry[key].add(video_id)

        # Step 2: labeled playlists
        playlist_stmt = (
            select(ResourceLabel.resource_id, ResourceLabel.label)
            .where(ResourceLabel.resource_type == "playlist")
            .where(ResourceLabel.label.in_({"whitelisted", "blacklisted"}))
        )
        for playlist_id, label in session.exec(playlist_stmt):
            if not playlist_id:
                continue
            key = "whitelisted_by" if label == "whitelisted" else "blacklisted_by"
            items_stmt = select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id)
            for item in session.exec(items_stmt):
                video_id = _extract_video_id_from_playlist_item(item.raw_json)
                if not video_id:
                    continue
                entry = _ensure_entry(video_id)
                entry[key].add(playlist_id)

        # Step 3: labeled channels and their uploads playlists
        channel_stmt = (
            select(Channel.id, Channel.uploads_playlist, ResourceLabel.label)
            .select_from(Channel)
            .join(
                ResourceLabel,
                and_(
                    ResourceLabel.resource_type == "channel",
                    ResourceLabel.resource_id == Channel.id,
                ),
            )
            .where(ResourceLabel.label.in_({"whitelisted", "blacklisted"}))
        )
        for channel_id, uploads_playlist, label in session.exec(channel_stmt):
            if not isinstance(channel_id, str) or not channel_id:
                continue
            if not isinstance(uploads_playlist, str) or not uploads_playlist:
                continue
            key = "whitelisted_by" if label == "whitelisted" else "blacklisted_by"
            items_stmt = select(PlaylistItem).where(
                PlaylistItem.playlist_id == uploads_playlist
            )
            for item in session.exec(items_stmt):
                video_id = _extract_video_id_from_playlist_item(item.raw_json)
                if not video_id:
                    continue
                entry = _ensure_entry(video_id)
                entry[key].add(channel_id)

        # Step 4: flagged videos
        flagged_stmt = select(ResourceLabel.resource_id).where(
            ResourceLabel.resource_type == "video",
            ResourceLabel.label == "flagged",
        )
        for (video_id,) in session.exec(flagged_stmt):
            if not video_id or video_id not in listings:
                continue
            entry = _ensure_entry(video_id)
            entry["disqualifying_attributes"].add("flagged")

        # Persist listings
        existing_entries = {
            row.video_id: row for row in session.exec(select(ListedVideo)).all()
        }

        processed_ids = set(listings.keys())
        for video_id, data in listings.items():
            whitelist_json = _dump_identifier_list(data["whitelisted_by"])
            blacklist_json = _dump_identifier_list(data["blacklisted_by"])
            disqualifying_json = _dump_identifier_list(
                data["disqualifying_attributes"]
            )
            entry = existing_entries.get(video_id)
            if entry:
                entry.whitelisted_by = whitelist_json
                entry.blacklisted_by = blacklist_json
                entry.disqualifying_attributes = disqualifying_json
            else:
                session.add(
                    ListedVideo(
                        video_id=video_id,
                        whitelisted_by=whitelist_json,
                        blacklisted_by=blacklist_json,
                        disqualifying_attributes=disqualifying_json,
                    )
                )

        if existing_entries:
            obsolete_ids = set(existing_entries.keys()) - processed_ids
            if obsolete_ids:
                session.exec(
                    delete(ListedVideo).where(ListedVideo.video_id.in_(obsolete_ids))
                )

        session.commit()


def fetch_all_videos() -> list[dict]:
    """Return stored video records including their labels."""

    engine = _get_engine()
    with Session(engine) as session:
        list_label = (
            select(ResourceLabel.label)
            .where(ResourceLabel.resource_type == "video")
            .where(ResourceLabel.resource_id == Video.id)
            .where(ResourceLabel.label.in_({"whitelisted", "blacklisted"}))
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            select(
                Video.id,
                Video.title,
                Video.channel_title,
                Video.retrieved_at,
                list_label,
                Video.raw_json,
                ListedVideo.disqualifying_attributes,
            )
            .select_from(Video)
            .join(ListedVideo, ListedVideo.video_id == Video.id, isouter=True)
            .order_by(list_label.is_(None), Video.retrieved_at.desc(), Video.id)
        )
        rows = session.exec(statement).all()
    return [
        {
            "id": row[0],
            "title": row[1],
            "channel_title": row[2],
            "retrieved_at": row[3],
            "label": row[4],
            "has_raw": bool(row[5]),
            "disqualifying_attributes": _load_identifier_list(row[6]),
        }
        for row in rows
    ]


def log_history_event(event_type: str, metadata: dict[str, Any] | None = None) -> None:
    """Persist a viewing history event in the database."""

    metadata = metadata or {}
    created_at = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(metadata, separators=(",", ":"))

    engine = _get_engine()
    with Session(engine) as session:
        session.add(
            HistoryEvent(
                event_type=event_type,
                created_at=created_at,
                metadata_json=payload,
            )
        )
        session.commit()


def fetch_history(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent history events."""

    engine = _get_engine()
    with Session(engine) as session:
        statement = (
            select(HistoryEvent)
            .order_by(HistoryEvent.created_at.desc())
            .limit(limit)
        )
        records = session.exec(statement).all()

    events: list[dict[str, Any]] = []
    for record in records:
        metadata: dict[str, Any]
        try:
            metadata = json.loads(record.metadata_json or "{}")
        except json.JSONDecodeError:
            metadata = {}
        events.append(
            {
                "id": record.id,
                "event_type": record.event_type,
                "created_at": record.created_at,
                "metadata": metadata,
            }
        )
    return events


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
    "clear_resource_label",
    "fetch_resource_label",
    "fetch_resource_labels_map",
    "fetch_listed_videos",
    "fetch_listed_video",
    "refresh_listed_video_disqualifications",
    "repopulate_listed_videos",
    "log_history_event",
    "fetch_history",
]

