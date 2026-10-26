"""Dividend provenance, tax components and corporate-action history."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("external_ref", sa.String(length=128), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "kind IN ('split', 'consolidation', 'rights_entitlement', "
            "'rights_take_up', 'rights_lapse', 'rename', 'delisting')",
            name=op.f("ck_corporate_actions_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_corporate_actions_instrument_id_instruments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolios.id"],
            name=op.f("fk_corporate_actions_portfolio_id_portfolios"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corporate_actions")),
        sa.UniqueConstraint(
            "instrument_id",
            "external_ref",
            name=op.f("uq_corporate_actions_instrument_id_external_ref"),
        ),
    )
    op.create_table(
        "dividend_suggestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("gross_amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("income_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "gross_amount <> 'NaN'", name=op.f("ck_dividend_suggestions_gross_amount_not_nan")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name=op.f("ck_dividend_suggestions_status_valid"),
        ),
        sa.CheckConstraint(
            "gross_amount >= 0", name=op.f("ck_dividend_suggestions_gross_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["income_id"],
            ["income.id"],
            name=op.f("fk_dividend_suggestions_income_id_income"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_dividend_suggestions_instrument_id_instruments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolios.id"],
            name=op.f("fk_dividend_suggestions_portfolio_id_portfolios"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dividend_suggestions")),
        sa.UniqueConstraint(
            "portfolio_id",
            "instrument_id",
            "ex_date",
            name=op.f("uq_dividend_suggestions_portfolio_id_instrument_id_ex_date"),
        ),
    )
    op.create_table(
        "import_income_matches",
        sa.Column("import_run_id", sa.Integer(), nullable=False),
        sa.Column("income_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["import_runs.id"],
            name=op.f("fk_import_income_matches_import_run_id_import_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["income_id"],
            ["income.id"],
            name=op.f("fk_import_income_matches_income_id_income"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "import_run_id", "income_id", name=op.f("pk_import_income_matches")
        ),
    )
    op.create_index(
        op.f("ix_import_income_matches_income_id"),
        "import_income_matches",
        ["income_id"],
        unique=False,
    )
    op.add_column(
        "income",
        sa.Column(
            "resident_withholding_tax",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "income",
        sa.Column(
            "foreign_withholding_tax",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "income",
        sa.Column(
            "franking_credits",
            sa.Numeric(precision=20, scale=6),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_income_foreign_withholding_tax_not_nan"),
        "income",
        "foreign_withholding_tax <> 'NaN'",
    )
    op.create_check_constraint(
        op.f("ck_income_franking_credits_not_nan"), "income", "franking_credits <> 'NaN'"
    )
    op.create_check_constraint(
        op.f("ck_income_rwt_nonnegative"), "income", "resident_withholding_tax >= 0"
    )
    op.create_check_constraint(
        op.f("ck_income_foreign_tax_nonnegative"), "income", "foreign_withholding_tax >= 0"
    )
    op.create_check_constraint(
        op.f("ck_income_franking_nonnegative"), "income", "franking_credits >= 0"
    )
    op.create_check_constraint(
        op.f("ck_income_resident_withholding_tax_not_nan"),
        "income",
        "resident_withholding_tax <> 'NaN'",
    )
    op.add_column("trades", sa.Column("income_id", sa.Integer(), nullable=True))
    op.add_column(
        "trades", sa.Column("consideration", sa.Numeric(precision=20, scale=6), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_trades_consideration_not_nan"), "trades", "consideration <> 'NaN'"
    )
    op.create_foreign_key(
        op.f("fk_trades_income_id_income"),
        "trades",
        "income",
        ["income_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_trades_consideration_nonnegative"),
        "trades",
        "consideration IS NULL OR consideration >= 0",
    )


def downgrade():
    op.drop_table("import_income_matches")
    op.drop_table("dividend_suggestions")
    op.drop_table("corporate_actions")
    op.drop_column("trades", "income_id")
    op.drop_column("trades", "consideration")
    for column in ("resident_withholding_tax", "foreign_withholding_tax", "franking_credits"):
        op.drop_column("income", column)
