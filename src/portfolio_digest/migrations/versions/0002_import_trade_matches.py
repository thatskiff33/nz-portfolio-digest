"""Track duplicate trades retained by later import runs.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_trade_matches",
        sa.Column("import_run_id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["import_runs.id"],
            name=op.f("fk_import_trade_matches_import_run_id_import_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"],
            ["trades.id"],
            name=op.f("fk_import_trade_matches_trade_id_trades"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("import_run_id", "trade_id", name=op.f("pk_import_trade_matches")),
    )
    op.create_index("ix_import_trade_matches_trade_id", "import_trade_matches", ["trade_id"])


def downgrade() -> None:
    op.drop_table("import_trade_matches")
