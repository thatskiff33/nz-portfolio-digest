"""share splits

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26 18:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "splits",
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("split_date", sa.Date(), nullable=False),
        sa.Column("shares_before", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("shares_after", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("shares_after <> 'NaN'", name=op.f("ck_splits_shares_after_not_nan")),
        sa.CheckConstraint("shares_after > 0", name=op.f("ck_splits_shares_after_positive")),
        sa.CheckConstraint("shares_before <> 'NaN'", name=op.f("ck_splits_shares_before_not_nan")),
        sa.CheckConstraint("shares_before > 0", name=op.f("ck_splits_shares_before_positive")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_splits_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instrument_id", "split_date", name=op.f("pk_splits")),
    )


def downgrade() -> None:
    op.drop_table("splits")
