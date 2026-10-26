"""Database tables.

Money and quantities use exact decimals (`Numeric`), never floats. Currencies are ISO 4217
codes. FX rates are stored as "1 unit of `currency` = `rate` units of the base currency"
(NZD by default), so converting a foreign amount to NZ$ is always a multiplication.

Fixed sets of values (trade types and so on) are plain strings guarded by CHECK
constraints, which keeps migrations simple and portable.

Financial history is never deleted as a side effect: removing a portfolio, instrument or
import run that still has trades or income is refused by the database.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from portfolio_digest.validation import PORTFOLIO_NAME_MAX

# Deterministic constraint names so migrations stay stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class ExactNumeric(TypeDecorator[Decimal]):
    """`Numeric` that refuses floats and NaN, so rounding errors can never reach the database."""

    impl = Numeric
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if isinstance(value, float):
            raise TypeError("Use Decimal, not float, for money, quantities and rates.")
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError(f"{value} is not a usable amount.")
        return value


# Quantities allow fractional shares (Sharesies sells fractions).
Quantity = ExactNumeric(24, 8)
Money = ExactNumeric(20, 6)
Rate = ExactNumeric(20, 10)

PORTFOLIO_NAME_UNIQUE = "uq_portfolios_name_lower"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _in(column: str, values: type[StrEnum]) -> str:
    quoted = ", ".join(f"'{v.value}'" for v in values)
    return f"{column} IN ({quoted})"


def _currency_ok(column: str) -> str:
    return f"{column} ~ '^[A-Z]{{3}}$'"


def _not_nan(*columns: str) -> list[CheckConstraint]:
    # Postgres sorts NaN above every number, so `price >= 0` alone would let it through.
    return [CheckConstraint(f"{c} <> 'NaN'", name=f"{c}_not_nan") for c in columns]


class InstrumentKind(StrEnum):
    SHARE = "share"
    ETF = "etf"
    FUND = "fund"
    RIGHTS = "rights"
    OTHER = "other"


class TradeType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    # Holdings brought in without a trade history (gifts, transfers in, starting position).
    OPENING_BALANCE = "opening_balance"
    # Units added or removed by a corporate action or correction (splits, DRP, transfers out).
    ADJUSTMENT = "adjustment"


class IncomeKind(StrEnum):
    DIVIDEND = "dividend"
    DISTRIBUTION = "distribution"
    INTEREST = "interest"
    OTHER = "other"


class ImportStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNDONE = "undone"


class Portfolio(Base):
    __tablename__ = "portfolios"
    __table_args__ = (
        CheckConstraint(_currency_ok("base_currency"), name="base_currency_iso"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(PORTFOLIO_NAME_MAX))
    base_currency: Mapped[str] = mapped_column(String(3), default="NZD", server_default="NZD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Deletes are left to the database, which refuses while the portfolio still has trades.
    trades: Mapped[list["Trade"]] = relationship(back_populates="portfolio", passive_deletes="all")


# "Personal" and "personal" would be confusing as two portfolios.
Index(PORTFOLIO_NAME_UNIQUE, func.lower(Portfolio.name), unique=True)


class Instrument(Base):
    """A listed security, identified by its exchange and the exchange's own code."""

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("exchange", "code"),
        CheckConstraint(_currency_ok("currency"), name="currency_iso"),
        CheckConstraint(_in("kind", InstrumentKind), name="kind_valid"),
        CheckConstraint("exchange = upper(exchange)", name="exchange_upper"),
        CheckConstraint("code = upper(code)", name="code_upper"),
        CheckConstraint("exchange ~ '^[^[:space:]]+$'", name="exchange_no_spaces"),
        CheckConstraint("code ~ '^[^[:space:]]+$'", name="code_no_spaces"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16))  # NZX, ASX, NASDAQ, NYSE...
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(String(16), default=InstrumentKind.SHARE.value)
    # Overrides the automatic Yahoo symbol (e.g. AIR.NZ) for odd cases.
    yahoo_symbol: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportRun(Base):
    """One import of a file or feed, so imports can be audited and undone."""

    __tablename__ = "import_runs"
    __table_args__ = (
        CheckConstraint(_in("status", ImportStatus), name="status_valid"),
        CheckConstraint("rows_imported >= 0", name="rows_imported_nonnegative"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_hex"),
        # Lets trades and income point at (id, portfolio_id), so a row can only belong to
        # an import into its own portfolio.
        UniqueConstraint("id", "portfolio_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"))
    source: Mapped[str] = mapped_column(String(64))  # e.g. sharesies_csv, asb_contract_note
    filename: Mapped[str | None] = mapped_column(String(255))
    # SHA-256 of the input, used to spot a file that has already been imported.
    content_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default=ImportStatus.RUNNING.value)
    rows_imported: Mapped[int] = mapped_column(default=0, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        # Re-importing the same broker record never creates a duplicate.
        UniqueConstraint("portfolio_id", "source", "external_ref"),
        CheckConstraint(_in("trade_type", TradeType), name="trade_type_valid"),
        CheckConstraint(_currency_ok("currency"), name="currency_iso"),
        CheckConstraint("quantity <> 0", name="quantity_nonzero"),
        CheckConstraint(
            "trade_type = 'adjustment' OR quantity > 0", name="quantity_positive_unless_adjustment"
        ),
        CheckConstraint("price >= 0", name="price_nonnegative"),
        CheckConstraint("fees >= 0", name="fees_nonnegative"),
        CheckConstraint("fx_rate IS NULL OR fx_rate > 0", name="fx_rate_positive"),
        CheckConstraint(
            "consideration IS NULL OR consideration >= 0", name="consideration_nonnegative"
        ),
        *_not_nan("quantity", "price", "fees", "fx_rate", "consideration"),
        Index("ix_trades_portfolio_instrument_date", "portfolio_id", "instrument_id", "trade_date"),
        # An import into another portfolio is refused. NULL means not imported.
        ForeignKeyConstraint(
            ["import_run_id", "portfolio_id"],
            ["import_runs.id", "import_runs.portfolio_id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"))
    trade_date: Mapped[date] = mapped_column(Date)
    trade_type: Mapped[str] = mapped_column(String(20))
    # Always positive for buys/sells; signed for adjustments.
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    # Price per unit in the trade currency.
    price: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    fees: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default="0")
    # Broker's own rate to the base currency, when it gives one.
    fx_rate: Mapped[Decimal | None] = mapped_column(Rate)
    source: Mapped[str] = mapped_column(String(64))  # e.g. sharesies, asb_securities, manual
    external_ref: Mapped[str] = mapped_column(String(128))
    import_run_id: Mapped[int | None] = mapped_column(index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    income_id: Mapped[int | None] = mapped_column(ForeignKey("income.id", ondelete="RESTRICT"))
    consideration: Mapped[Decimal | None] = mapped_column(Money)

    portfolio: Mapped[Portfolio] = relationship(back_populates="trades")
    instrument: Mapped[Instrument] = relationship()


class ImportTradeMatch(Base):
    """A later run that independently supplied an already stored trade."""

    __tablename__ = "import_trade_matches"

    import_run_id: Mapped[int] = mapped_column(
        ForeignKey("import_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Income(Base):
    """Cash paid to the holder: dividends, fund distributions, interest."""

    __tablename__ = "income"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "source", "external_ref"),
        CheckConstraint(_in("kind", IncomeKind), name="kind_valid"),
        CheckConstraint(_currency_ok("currency"), name="currency_iso"),
        CheckConstraint("gross_amount >= 0", name="gross_nonnegative"),
        CheckConstraint("net_amount >= 0", name="net_nonnegative"),
        CheckConstraint("imputation_credits >= 0", name="imputation_nonnegative"),
        CheckConstraint("withholding_tax >= 0", name="withholding_nonnegative"),
        CheckConstraint("drp_quantity IS NULL OR drp_quantity > 0", name="drp_quantity_positive"),
        CheckConstraint("fx_rate IS NULL OR fx_rate > 0", name="fx_rate_positive"),
        CheckConstraint("net_amount <= gross_amount", name="net_not_above_gross"),
        CheckConstraint("withholding_tax <= gross_amount", name="withholding_not_above_gross"),
        CheckConstraint("resident_withholding_tax >= 0", name="rwt_nonnegative"),
        CheckConstraint("foreign_withholding_tax >= 0", name="foreign_tax_nonnegative"),
        CheckConstraint("franking_credits >= 0", name="franking_nonnegative"),
        *_not_nan(
            "resident_withholding_tax",
            "foreign_withholding_tax",
            "franking_credits",
            "gross_amount",
            "net_amount",
            "imputation_credits",
            "withholding_tax",
            "drp_quantity",
            "fx_rate",
        ),
        Index("ix_income_portfolio_instrument_paid", "portfolio_id", "instrument_id", "paid_date"),
        # An import into another portfolio is refused. NULL means not imported.
        ForeignKeyConstraint(
            ["import_run_id", "portfolio_id"],
            ["import_runs.id", "import_runs.portfolio_id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16), default=IncomeKind.DIVIDEND.value)
    ex_date: Mapped[date | None] = mapped_column(Date)
    paid_date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3))
    # Cash declared before any tax is withheld, not counting imputation credits.
    gross_amount: Mapped[Decimal] = mapped_column(Money)
    # Cash actually received: gross less withholding tax (RWT, NRWT or foreign tax).
    net_amount: Mapped[Decimal] = mapped_column(Money)
    imputation_credits: Mapped[Decimal] = mapped_column(
        Money, default=Decimal(0), server_default="0"
    )
    withholding_tax: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default="0")
    resident_withholding_tax: Mapped[Decimal] = mapped_column(
        Money, default=Decimal(0), server_default="0"
    )
    foreign_withholding_tax: Mapped[Decimal] = mapped_column(
        Money, default=Decimal(0), server_default="0"
    )
    franking_credits: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), server_default="0")
    # Units received instead of cash under a dividend reinvestment plan.
    drp_quantity: Mapped[Decimal | None] = mapped_column(Quantity)
    fx_rate: Mapped[Decimal | None] = mapped_column(Rate)
    source: Mapped[str] = mapped_column(String(64))
    external_ref: Mapped[str] = mapped_column(String(128))
    import_run_id: Mapped[int | None] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Price(Base):
    """Daily closing price in the instrument's own currency."""

    __tablename__ = "prices"
    __table_args__ = (
        CheckConstraint("close >= 0", name="close_nonnegative"),
        *_not_nan("close"),
    )

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    price_date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str] = mapped_column(String(32))  # e.g. yahoo
    # True when no fresh price was available and the last known close was carried forward.
    is_stale: Mapped[bool] = mapped_column(default=False, server_default="false")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FxRate(Base):
    """Daily rate: 1 unit of `currency` is worth `rate` units of `base_currency`."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        CheckConstraint(_currency_ok("currency"), name="currency_iso"),
        CheckConstraint(_currency_ok("base_currency"), name="base_currency_iso"),
        CheckConstraint("currency <> base_currency", name="different_currencies"),
        CheckConstraint("rate > 0", name="rate_positive"),
        *_not_nan("rate"),
    )

    rate_date: Mapped[date] = mapped_column(Date, primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Rate)
    source: Mapped[str] = mapped_column(String(32))  # e.g. yahoo, frankfurter
    is_stale: Mapped[bool] = mapped_column(default=False, server_default="false")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Split(Base):
    """A share split or consolidation: `shares_before` old shares became `shares_after` new ones.

    A 4-for-1 split is 1 before and 4 after; a 1-for-10 consolidation is 10 before and 1 after.
    Recorded from the price source so a big price move on that day is not flagged as bad data.
    """

    __tablename__ = "splits"
    __table_args__ = (
        CheckConstraint("shares_before > 0", name="shares_before_positive"),
        CheckConstraint("shares_after > 0", name="shares_after_positive"),
        *_not_nan("shares_before", "shares_after"),
    )

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    split_date: Mapped[date] = mapped_column(Date, primary_key=True)
    shares_before: Mapped[Decimal] = mapped_column(Quantity)
    shares_after: Mapped[Decimal] = mapped_column(Quantity)
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CorporateAction(Base):
    """Immutable instrument history; details retain former codes and event terms."""

    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "external_ref"),
        CheckConstraint(
            "kind IN ('split', 'consolidation', 'rights_entitlement', "
            "'rights_take_up', 'rights_lapse', 'rename', 'delisting')",
            name="kind_valid",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"))
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="RESTRICT")
    )
    effective_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(32))
    external_ref: Mapped[str] = mapped_column(String(128))
    details: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64), default="registry")


class DividendSuggestion(Base):
    """Yahoo estimates remain outside income until explicitly confirmed."""

    __tablename__ = "dividend_suggestions"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "instrument_id", "ex_date"),
        CheckConstraint("status IN ('pending', 'confirmed', 'rejected')", name="status_valid"),
        CheckConstraint("gross_amount >= 0", name="gross_nonnegative"),
        *_not_nan("gross_amount"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="RESTRICT"))
    ex_date: Mapped[date] = mapped_column(Date)
    gross_amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    income_id: Mapped[int | None] = mapped_column(ForeignKey("income.id", ondelete="RESTRICT"))


class ImportIncomeMatch(Base):
    """Retain independently imported income when an earlier import is undone."""

    __tablename__ = "import_income_matches"
    import_run_id: Mapped[int] = mapped_column(
        ForeignKey("import_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    income_id: Mapped[int] = mapped_column(
        ForeignKey("income.id", ondelete="CASCADE"), primary_key=True, index=True
    )
