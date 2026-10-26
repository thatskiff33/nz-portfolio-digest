"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-26 16:05:12.225581
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("is_stale", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$'", name=op.f("ck_fx_rates_base_currency_iso")
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_fx_rates_currency_iso")),
        sa.CheckConstraint("rate <> 'NaN'", name=op.f("ck_fx_rates_rate_not_nan")),
        sa.CheckConstraint(
            "currency <> base_currency", name=op.f("ck_fx_rates_different_currencies")
        ),
        sa.CheckConstraint("rate > 0", name=op.f("ck_fx_rates_rate_positive")),
        sa.PrimaryKeyConstraint("rate_date", "currency", "base_currency", name=op.f("pk_fx_rates")),
    )
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("yahoo_symbol", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("code ~ '^[^[:space:]]+$'", name=op.f("ck_instruments_code_no_spaces")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_instruments_currency_iso")),
        sa.CheckConstraint(
            "exchange ~ '^[^[:space:]]+$'", name=op.f("ck_instruments_exchange_no_spaces")
        ),
        sa.CheckConstraint(
            "kind IN ('share', 'etf', 'fund', 'rights', 'other')",
            name=op.f("ck_instruments_kind_valid"),
        ),
        sa.CheckConstraint("code = upper(code)", name=op.f("ck_instruments_code_upper")),
        sa.CheckConstraint(
            "exchange = upper(exchange)", name=op.f("ck_instruments_exchange_upper")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruments")),
        sa.UniqueConstraint("exchange", "code", name=op.f("uq_instruments_exchange_code")),
    )
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("base_currency", sa.String(length=3), server_default="NZD", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$'", name=op.f("ck_portfolios_base_currency_iso")
        ),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_portfolios_name_not_blank")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portfolios")),
    )
    op.create_index(
        "uq_portfolios_name_lower", "portfolios", [sa.literal_column("lower(name)")], unique=True
    )
    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rows_imported", sa.Integer(), server_default="0", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_import_runs_content_sha256_hex")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'undone')",
            name=op.f("ck_import_runs_status_valid"),
        ),
        sa.CheckConstraint(
            "rows_imported >= 0", name=op.f("ck_import_runs_rows_imported_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolios.id"],
            name=op.f("fk_import_runs_portfolio_id_portfolios"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_runs")),
    )
    op.create_index(
        op.f("ix_import_runs_content_sha256"), "import_runs", ["content_sha256"], unique=False
    )
    op.create_table(
        "prices",
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("close", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("is_stale", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("close <> 'NaN'", name=op.f("ck_prices_close_not_nan")),
        sa.CheckConstraint("close >= 0", name=op.f("ck_prices_close_nonnegative")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_prices_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instrument_id", "price_date", name=op.f("pk_prices")),
    )
    op.create_table(
        "income",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("paid_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("net_amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "imputation_credits",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "withholding_tax", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False
        ),
        sa.Column("drp_quantity", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("fx_rate", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_ref", sa.String(length=128), nullable=False),
        sa.Column("import_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_income_currency_iso")),
        sa.CheckConstraint("drp_quantity <> 'NaN'", name=op.f("ck_income_drp_quantity_not_nan")),
        sa.CheckConstraint("fx_rate <> 'NaN'", name=op.f("ck_income_fx_rate_not_nan")),
        sa.CheckConstraint("gross_amount <> 'NaN'", name=op.f("ck_income_gross_amount_not_nan")),
        sa.CheckConstraint(
            "imputation_credits <> 'NaN'", name=op.f("ck_income_imputation_credits_not_nan")
        ),
        sa.CheckConstraint(
            "kind IN ('dividend', 'distribution', 'interest', 'other')",
            name=op.f("ck_income_kind_valid"),
        ),
        sa.CheckConstraint("net_amount <> 'NaN'", name=op.f("ck_income_net_amount_not_nan")),
        sa.CheckConstraint(
            "withholding_tax <> 'NaN'", name=op.f("ck_income_withholding_tax_not_nan")
        ),
        sa.CheckConstraint(
            "drp_quantity IS NULL OR drp_quantity > 0", name=op.f("ck_income_drp_quantity_positive")
        ),
        sa.CheckConstraint(
            "fx_rate IS NULL OR fx_rate > 0", name=op.f("ck_income_fx_rate_positive")
        ),
        sa.CheckConstraint("gross_amount >= 0", name=op.f("ck_income_gross_nonnegative")),
        sa.CheckConstraint(
            "imputation_credits >= 0", name=op.f("ck_income_imputation_nonnegative")
        ),
        sa.CheckConstraint(
            "net_amount <= gross_amount", name=op.f("ck_income_net_not_above_gross")
        ),
        sa.CheckConstraint("net_amount >= 0", name=op.f("ck_income_net_nonnegative")),
        sa.CheckConstraint(
            "withholding_tax <= gross_amount", name=op.f("ck_income_withholding_not_above_gross")
        ),
        sa.CheckConstraint("withholding_tax >= 0", name=op.f("ck_income_withholding_nonnegative")),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["import_runs.id"],
            name=op.f("fk_income_import_run_id_import_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_income_instrument_id_instruments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolios.id"],
            name=op.f("fk_income_portfolio_id_portfolios"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_income")),
        sa.UniqueConstraint(
            "portfolio_id",
            "source",
            "external_ref",
            name=op.f("uq_income_portfolio_id_source_external_ref"),
        ),
    )
    op.create_index(op.f("ix_income_import_run_id"), "income", ["import_run_id"], unique=False)
    op.create_index(
        "ix_income_portfolio_instrument_paid",
        "income",
        ["portfolio_id", "instrument_id", "paid_date"],
        unique=False,
    )
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("trade_type", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fees", sa.Numeric(precision=20, scale=6), server_default="0", nullable=False),
        sa.Column("fx_rate", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_ref", sa.String(length=128), nullable=False),
        sa.Column("import_run_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_trades_currency_iso")),
        sa.CheckConstraint("fees <> 'NaN'", name=op.f("ck_trades_fees_not_nan")),
        sa.CheckConstraint("fx_rate <> 'NaN'", name=op.f("ck_trades_fx_rate_not_nan")),
        sa.CheckConstraint("price <> 'NaN'", name=op.f("ck_trades_price_not_nan")),
        sa.CheckConstraint("quantity <> 'NaN'", name=op.f("ck_trades_quantity_not_nan")),
        sa.CheckConstraint(
            "trade_type = 'adjustment' OR quantity > 0",
            name=op.f("ck_trades_quantity_positive_unless_adjustment"),
        ),
        sa.CheckConstraint(
            "trade_type IN ('buy', 'sell', 'opening_balance', 'adjustment')",
            name=op.f("ck_trades_trade_type_valid"),
        ),
        sa.CheckConstraint("fees >= 0", name=op.f("ck_trades_fees_nonnegative")),
        sa.CheckConstraint(
            "fx_rate IS NULL OR fx_rate > 0", name=op.f("ck_trades_fx_rate_positive")
        ),
        sa.CheckConstraint("price >= 0", name=op.f("ck_trades_price_nonnegative")),
        sa.CheckConstraint("quantity <> 0", name=op.f("ck_trades_quantity_nonzero")),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["import_runs.id"],
            name=op.f("fk_trades_import_run_id_import_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_trades_instrument_id_instruments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolios.id"],
            name=op.f("fk_trades_portfolio_id_portfolios"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trades")),
        sa.UniqueConstraint(
            "portfolio_id",
            "source",
            "external_ref",
            name=op.f("uq_trades_portfolio_id_source_external_ref"),
        ),
    )
    op.create_index(op.f("ix_trades_import_run_id"), "trades", ["import_run_id"], unique=False)
    op.create_index(
        "ix_trades_portfolio_instrument_date",
        "trades",
        ["portfolio_id", "instrument_id", "trade_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_trades_portfolio_instrument_date", table_name="trades")
    op.drop_index(op.f("ix_trades_import_run_id"), table_name="trades")
    op.drop_table("trades")
    op.drop_index("ix_income_portfolio_instrument_paid", table_name="income")
    op.drop_index(op.f("ix_income_import_run_id"), table_name="income")
    op.drop_table("income")
    op.drop_table("prices")
    op.drop_index(op.f("ix_import_runs_content_sha256"), table_name="import_runs")
    op.drop_table("import_runs")
    op.drop_index("uq_portfolios_name_lower", table_name="portfolios")
    op.drop_table("portfolios")
    op.drop_table("instruments")
    op.drop_table("fx_rates")
