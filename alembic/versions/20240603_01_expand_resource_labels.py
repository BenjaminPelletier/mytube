"""Expand resource labels allowed values"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20240603_01"
down_revision = "20240527_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("resource_labels", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_resource_labels_label", type_="check")
        batch_op.create_check_constraint(
            "ck_resource_labels_label",
            "label IN ('whitelisted', 'blacklisted', 'favorite', 'flagged')",
        )


def downgrade() -> None:
    with op.batch_alter_table("resource_labels", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_resource_labels_label", type_="check")
        batch_op.create_check_constraint(
            "ck_resource_labels_label",
            "label IN ('whitelisted', 'blacklisted')",
        )
