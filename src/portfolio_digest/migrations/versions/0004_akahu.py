"""Akahu manual mappings and confirmation snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "akahu_mappings",
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("holding_id", sa.String(128), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("portfolio_id", "account_id", "holding_id"),
    )
    op.create_table(
        "akahu_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("observations", sa.Text(), nullable=False),
        sa.Column("report", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'matched', 'drift', 'failed')", name="status_valid"
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("akahu_snapshots")
    op.drop_table("akahu_mappings")
