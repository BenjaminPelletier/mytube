"""Add channel_title column to videos."""

from __future__ import annotations

import json
from collections.abc import Iterable

from alembic import op
import sqlalchemy as sa
from sqlalchemy import orm


# revision identifiers, used by Alembic.
revision = "20240608_01"
down_revision = "20240527_01"
branch_labels = None
depends_on = None


videos_table = sa.table(
    "videos",
    sa.column("id", sa.String()),
    sa.column("raw_json", sa.Text()),
    sa.column("channel_title", sa.String()),
)

channels_table = sa.table(
    "channels",
    sa.column("id", sa.String()),
    sa.column("title", sa.String()),
    sa.column("uploads_playlist", sa.String()),
)

playlist_items_table = sa.table(
    "playlist_items",
    sa.column("playlist_id", sa.String()),
    sa.column("raw_json", sa.Text()),
)


def _extract_channel_title(raw_json: str | None) -> str | None:
    if not raw_json:
        return None
    try:
        payload: object = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    snippet = payload.get("snippet")
    if not isinstance(snippet, dict):
        return None
    channel_title = snippet.get("channelTitle")
    if isinstance(channel_title, str) and channel_title.strip():
        return channel_title
    return None


def _extract_video_id_from_playlist_item(raw_json: str | None) -> str | None:
    if not raw_json:
        return None
    try:
        payload: object = json.loads(raw_json)
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


def _update_channel_titles(
    session: orm.Session, updates: Iterable[tuple[str, str]]
) -> None:
    for video_id, channel_title in updates:
        session.execute(
            videos_table.update()
            .where(videos_table.c.id == video_id)
            .values(channel_title=channel_title)
        )


def upgrade() -> None:
    op.add_column("videos", sa.Column("channel_title", sa.String(), nullable=True))

    bind = op.get_bind()
    session = orm.Session(bind=bind)

    try:
        # Populate from stored video raw payloads first
        payload_updates: list[tuple[str, str]] = []
        for video_id, raw_json in session.execute(
            sa.select(videos_table.c.id, videos_table.c.raw_json)
        ):
            channel_title = _extract_channel_title(raw_json)
            if channel_title:
                payload_updates.append((video_id, channel_title))
        if payload_updates:
            _update_channel_titles(session, payload_updates)
        session.flush()

        # Determine which videos still need a channel title
        missing_video_ids = {
            video_id
            for (video_id,) in session.execute(
                sa.select(videos_table.c.id).where(
                    sa.or_(
                        videos_table.c.channel_title.is_(None),
                        videos_table.c.channel_title == "",
                    )
                )
            )
        }
        if not missing_video_ids:
            session.commit()
            return

        playlist_updates: dict[str, str] = {}
        for channel_title, uploads_playlist in session.execute(
            sa.select(
                channels_table.c.title,
                channels_table.c.uploads_playlist,
            )
        ):
            if not channel_title or not uploads_playlist:
                continue
            for (raw_json,) in session.execute(
                sa.select(playlist_items_table.c.raw_json).where(
                    playlist_items_table.c.playlist_id == uploads_playlist
                )
            ):
                video_id = _extract_video_id_from_playlist_item(raw_json)
                if (
                    video_id
                    and video_id in missing_video_ids
                    and video_id not in playlist_updates
                ):
                    playlist_updates[video_id] = channel_title
        if playlist_updates:
            _update_channel_titles(session, playlist_updates.items())

        session.commit()
    finally:
        session.close()


def downgrade() -> None:
    op.drop_column("videos", "channel_title")
