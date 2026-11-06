"""Index income references and track successful split scans."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_trades_income_id", "trades", ["income_id"])
    op.create_index("ix_dividend_suggestions_income_id", "dividend_suggestions", ["income_id"])
    op.add_column("instruments", sa.Column("split_scanned_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("instruments", "split_scanned_at")
    op.drop_index("ix_dividend_suggestions_income_id", table_name="dividend_suggestions")
    op.drop_index("ix_trades_income_id", table_name="trades")
