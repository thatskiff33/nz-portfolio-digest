"""Persistent alert delivery state."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "alert_states",
        sa.Column("portfolio_id", sa.Integer(), sa.ForeignKey("portfolios.id"), primary_key=True),
        sa.Column("rule_key", sa.String(256), primary_key=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("alert_states")
