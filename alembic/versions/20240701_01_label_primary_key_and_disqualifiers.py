from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20240701_01"
down_revision = "20240608_01"
branch_labels = None
depends_on = None


CHECK_CONSTRAINT_NAME = "ck_resource_labels_label"
PRIMARY_KEY_NAME = "pk_resource_labels"


def upgrade() -> None:
    op.add_column(
        "listed_videos",
        sa.Column("disqualifying_attributes", sa.Text(), nullable=True),
    )

    op.create_table(
        "resource_labels__tmp",
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint(
            "resource_type", "resource_id", "label", name=PRIMARY_KEY_NAME
        ),
        sa.CheckConstraint(
            "label IN ('whitelisted', 'blacklisted', 'favorite', 'flagged')",
            name=CHECK_CONSTRAINT_NAME,
        ),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO resource_labels__tmp (resource_type, resource_id, label)
            SELECT
                CASE
                    WHEN resource_type IN ('video.favorite', 'video.flagged')
                        THEN 'video'
                    ELSE resource_type
                END AS resource_type,
                resource_id,
                label
            FROM resource_labels
            """
        )
    )

    op.drop_table("resource_labels")
    op.rename_table("resource_labels__tmp", "resource_labels")


def downgrade() -> None:
    op.create_table(
        "resource_labels__tmp",
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("resource_type", "resource_id", name=PRIMARY_KEY_NAME),
        sa.CheckConstraint(
            "label IN ('whitelisted', 'blacklisted', 'favorite', 'flagged')",
            name=CHECK_CONSTRAINT_NAME,
        ),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO resource_labels__tmp (resource_type, resource_id, label)
            SELECT resource_type, resource_id, MIN(label) AS label
            FROM (
                SELECT
                    CASE
                        WHEN resource_type = 'video' AND label = 'favorite'
                            THEN 'video.favorite'
                        WHEN resource_type = 'video' AND label = 'flagged'
                            THEN 'video.flagged'
                        ELSE resource_type
                    END AS resource_type,
                    resource_id,
                    label
                FROM resource_labels
            )
            GROUP BY resource_type, resource_id
            """
        )
    )

    op.drop_table("resource_labels")
    op.rename_table("resource_labels__tmp", "resource_labels")

    op.drop_column("listed_videos", "disqualifying_attributes")
