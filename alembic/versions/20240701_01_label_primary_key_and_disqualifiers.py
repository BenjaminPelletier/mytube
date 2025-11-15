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
    with op.batch_alter_table("listed_videos", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("disqualifying_attributes", sa.Text(), nullable=True))

    with op.batch_alter_table("resource_labels", recreate="always") as batch_op:
        batch_op.drop_constraint(CHECK_CONSTRAINT_NAME, type_="check")
        batch_op.drop_constraint(None, type_="primary")
        batch_op.create_primary_key(
            PRIMARY_KEY_NAME, ["resource_type", "resource_id", "label"]
        )
        batch_op.create_check_constraint(
            CHECK_CONSTRAINT_NAME,
            "label IN ('whitelisted', 'blacklisted', 'favorite', 'flagged')",
        )

    op.execute(
        sa.text(
            """
            UPDATE resource_labels
            SET resource_type = 'video'
            WHERE resource_type IN ('video.favorite', 'video.flagged')
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE resource_labels
            SET resource_type = 'video.favorite'
            WHERE resource_type = 'video' AND label = 'favorite'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE resource_labels
            SET resource_type = 'video.flagged'
            WHERE resource_type = 'video' AND label = 'flagged'
            """
        )
    )

    op.execute(
        sa.text(
            """
            DELETE FROM resource_labels
            WHERE rowid NOT IN (
                SELECT MIN(rowid)
                FROM resource_labels
                GROUP BY resource_type, resource_id
            )
            """
        )
    )

    with op.batch_alter_table("resource_labels", recreate="always") as batch_op:
        batch_op.drop_constraint(CHECK_CONSTRAINT_NAME, type_="check")
        batch_op.drop_constraint(None, type_="primary")
        batch_op.create_primary_key(PRIMARY_KEY_NAME, ["resource_type", "resource_id"])
        batch_op.create_check_constraint(
            CHECK_CONSTRAINT_NAME,
            "label IN ('whitelisted', 'blacklisted', 'favorite', 'flagged')",
        )

    with op.batch_alter_table("listed_videos", recreate="always") as batch_op:
        batch_op.drop_column("disqualifying_attributes")
