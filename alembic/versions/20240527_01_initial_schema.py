"""Create initial mytube schema"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20240527_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.String(), nullable=False),
        sa.Column("uploads_playlist", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_channels_uploads_playlist", "channels", ["uploads_playlist"], unique=False)

    op.create_table(
        "channel_sections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_channel_sections_channel_id", "channel_sections", ["channel_id"], unique=False)

    op.create_table(
        "history_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_history_events_created_at", "history_events", ["created_at"], unique=False)

    op.create_table(
        "listed_videos",
        sa.Column("video_id", sa.String(), nullable=False),
        sa.Column("whitelisted_by", sa.Text(), nullable=True),
        sa.Column("blacklisted_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("video_id"),
    )

    op.create_table(
        "playlist_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("playlist_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("published_at", sa.String(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_playlist_items_playlist_id", "playlist_items", ["playlist_id"], unique=False)

    op.create_table(
        "playlists",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "resource_labels",
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.CheckConstraint(
            "label IN ('whitelisted', 'blacklisted')",
            name="ck_resource_labels_label",
        ),
        sa.PrimaryKeyConstraint("resource_type", "resource_id"),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "videos",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("videos")
    op.drop_table("settings")
    op.drop_table("resource_labels")
    op.drop_table("playlists")
    op.drop_index("ix_playlist_items_playlist_id", table_name="playlist_items")
    op.drop_table("playlist_items")
    op.drop_table("listed_videos")
    op.drop_index("ix_history_events_created_at", table_name="history_events")
    op.drop_table("history_events")
    op.drop_index("ix_channel_sections_channel_id", table_name="channel_sections")
    op.drop_table("channel_sections")
    op.drop_index("ix_channels_uploads_playlist", table_name="channels")
    op.drop_table("channels")
