"""Made-up broker records and economic invariants for dividends and actions."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from portfolio_digest.actions import record_action
from portfolio_digest.cli import build_parser
from portfolio_digest.dividends import decide, suggest
from portfolio_digest.importers.asb_dividends import AsbDividendImporter
from portfolio_digest.importers.base import FileProblem, ParseContext, RowProblem
from portfolio_digest.importers.sharesies import SharesiesImporter
from portfolio_digest.importing import ImportFailure, import_file, undo_import
from portfolio_digest.income import parse_income, store_income
from portfolio_digest.models import (
    CorporateAction,
    Income,
    Instrument,
    Portfolio,
    Price,
    Split,
    Trade,
)
from portfolio_digest.prices.sources import YahooSource
from portfolio_digest.reconcile import stored_units
from portfolio_digest.returns import instrument_position, position, report

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures"
START, EX, PAID = date(2024, 1, 1), date(2024, 6, 1), date(2024, 6, 20)


def parsed():
    return (
        AsbDividendImporter()
        .parse((FIXTURES / "asb-dividend.txt").read_bytes(), ParseContext())
        .incomes[0]
    )


def test_sharesies_reads_tax_components_and_etf_reinvestment():
    result = SharesiesImporter().parse(
        (FIXTURES / "sharesies-income.csv").read_bytes(), ParseContext()
    )
    assert result.errors == []
    nz, us, au, drp = result.incomes
    assert (nz.gross_amount, nz.net_amount, nz.resident_withholding_tax, nz.imputation_credits) == (
        D(100),
        D(90),
        D(10),
        D(28),
    )
    assert us.foreign_withholding_tax / us.gross_amount == D("0.15")
    assert us.fx_rate == D("1.6")
    assert au.franking_credits == D("42.857143")
    assert au.imputation_credits == 0
    assert drp.kind == "distribution" and drp.drp_quantity == 3


def test_asb_extracted_text_and_email_give_same_income():
    body = (FIXTURES / "asb-dividend.txt").read_bytes()
    email = b"Subject: Fictional dividend\nContent-Type: text/plain; charset=utf-8\n\n" + body
    importer = AsbDividendImporter()
    assert importer.parse(email, ParseContext()).incomes == [parsed()]


@pytest.mark.parametrize(
    "old,new",
    [
        (b"90.00", b"91.00"),
        (b"10.00", b"-10.00"),
        (b"100.00", b"NaN"),
        (b"DRP quantity: 3", b"DRP quantity: 0"),
    ],
)
def test_asb_refuses_inconsistent_or_invalid_income(old, new):
    content = (FIXTURES / "asb-dividend.txt").read_bytes().replace(old, new)
    assert AsbDividendImporter().parse(content, ParseContext()).errors


def test_missing_gross_is_an_error_rather_than_silently_skipped_income():
    content = (FIXTURES / "sharesies-income.csv").read_bytes().replace(b",100,10,10,", b",,10,10,")
    result = SharesiesImporter().parse(content, ParseContext())
    assert len(result.errors) == 2
    assert not result.skipped


def test_pdf_requires_explicit_text_extraction():
    with pytest.raises(FileProblem, match="Extract its text"):
        AsbDividendImporter().parse(b"%PDF fake", ParseContext())


def trade(day=START, quantity="100", price="10", kind="buy"):
    return SimpleNamespace(
        trade_date=day,
        quantity=D(quantity),
        price=D(price),
        fees=D(0),
        trade_type=kind,
        consideration=None,
    )


@pytest.mark.parametrize(
    "before,after,units,price", [("1", "2", "200", "5"), ("10", "1", "10", "100")]
)
def test_split_and_consolidation_preserve_total_cost_and_return(before, after, units, price):
    split = SimpleNamespace(split_date=EX, shares_before=D(before), shares_after=D(after))
    initial = position([trade()], [], START)
    changed = position([trade()], [split], PAID)
    assert changed.units == D(units)
    assert changed.cost_base == initial.cost_base == D(1000)
    assert changed.units * D(price) - changed.purchases == 0
    assert position([trade()], [split], START).units == 100


def test_ex_date_entitlement_excludes_same_day_purchases_and_respects_split():
    split = SimpleNamespace(split_date=EX, shares_before=D(1), shares_after=D(2))
    assert position([trade(), trade(EX)], [split], EX, before_trades=True).units == 200
    assert position([trade(), trade(EX)], [split], EX).units == 300


def test_split_precedes_same_day_sale_and_preserves_remaining_cost():
    split = SimpleNamespace(split_date=EX, shares_before=D(1), shares_after=D(2))
    result = position([trade(), trade(EX, "50", "6", "sell")], [split], PAID)
    assert (result.units, result.cost_base, result.proceeds) == (150, 750, 300)


def test_oversell_is_explicit_error():
    with pytest.raises(ImportFailure, match="exceed holdings"):
        position([trade(quantity="2", kind="sell")], [], PAID)


def seed(session):
    portfolio = Portfolio(name="Fictional", base_currency="NZD")
    instrument = Instrument(exchange="NZX", code="EXM", name="Example", currency="NZD")
    session.add_all([portfolio, instrument])
    session.flush()
    session.add(
        Trade(
            portfolio_id=portfolio.id,
            instrument_id=instrument.id,
            trade_date=START,
            trade_type="buy",
            quantity=D(100),
            price=D(10),
            currency="NZD",
            source="fictional",
            external_ref="fictional-1",
        )
    )
    session.add(Price(instrument_id=instrument.id, price_date=PAID, close=D(10), source="fixture"))
    session.flush()
    return portfolio, instrument


def test_drp_link_exact_cost_and_gross_up_toggle(engine):
    with Session(engine) as session, session.begin():
        portfolio, instrument = seed(session)
        income, _ = store_income(session, portfolio.id, instrument.id, parsed(), "registry")
        session.flush()
        purchase = session.scalar(select(Trade).where(Trade.income_id == income.id))
        assert (purchase.quantity, purchase.consideration, purchase.price) == (3, 90, 30)
        row = report(session, portfolio, PAID)[0]
        assert (row.units, row.cost_base, row.dividends, row.total_return) == (103, 1090, 90, 30)
        assert row.imputation_gross_up == 28
        assert report(session, portfolio, PAID, gross_up=True)[0].total_return == 58


def test_income_import_repeat_undo_preserves_independent_evidence_and_drp(engine):
    with Session(engine) as session:
        portfolio, _ = seed(session)
        session.commit()
        path = FIXTURES / "asb-dividend.txt"
        first = import_file(session, "asb-dividend", path, portfolio.name)
        second = import_file(session, "asb-dividend", path, portfolio.name)
        assert (first.imported, second.imported, second.duplicates) == (1, 0, 1)
        assert undo_import(session, first.run_id) == 0
        assert session.scalar(select(func.count()).select_from(Income)) == 1
        assert undo_import(session, second.run_id) == 2
        assert session.scalar(select(func.count()).select_from(Income)) == 0
        assert session.scalar(select(func.count()).select_from(Trade)) == 1


def test_failed_income_import_rolls_back_prior_income_and_trades(engine, tmp_path):
    with Session(engine) as session:
        portfolio, instrument = seed(session)
        store_income(session, portfolio.id, instrument.id, parsed(), "asb_securities")
        session.commit()
        path = tmp_path / "conflicting.txt"
        path.write_bytes((FIXTURES / "asb-dividend.txt").read_bytes().replace(b"28.00", b"29.00"))
        with pytest.raises(ImportFailure, match="different fields"):
            import_file(session, "asb-dividend", path, portfolio.name)
        assert session.scalar(select(func.count()).select_from(Income)) == 1
        assert session.scalar(select(func.count()).select_from(Trade)) == 2


def test_suggestions_never_affect_returns_until_confirmed_and_reject_is_sticky(engine):
    with Session(engine) as session, session.begin():
        portfolio, instrument = seed(session)
        items = suggest(session, portfolio, instrument, {EX: D(1), PAID: D("0.5")})
        assert report(session, portfolio, PAID)[0].dividends == 0
        with pytest.raises(RowProblem, match="evidence"):
            decide(session, items[0].id)
        decide(
            session,
            items[0].id,
            confirmed=replace(parsed(), drp_quantity=None),
            evidence="registry",
        )
        decide(session, items[1].id, reject=True)
        session.flush()
        assert report(session, portfolio, PAID)[0].dividends == 90
        assert [
            i.status for i in suggest(session, portfolio, instrument, {EX: D(1), PAID: D(1)})
        ] == ["confirmed", "rejected"]
        with pytest.raises(ImportFailure, match="already"):
            decide(session, items[0].id, reject=True)


@pytest.mark.parametrize(
    "kind,before,after,units,price",
    [
        ("split", "1", "2", 200, "5"),
        ("consolidation", "10", "1", 10, "100"),
    ],
)
def test_recorded_action_changes_holdings_cost_and_returns_once(
    engine, kind, before, after, units, price
):
    with Session(engine) as session, session.begin():
        portfolio, instrument = seed(session)
        record_action(
            session, instrument, EX, kind, "fictional-split", {"before": before, "after": after}
        )
        session.get(Price, (instrument.id, PAID)).close = D(price)
        session.flush()
        row = report(session, portfolio, PAID)[0]
        assert (row.units, row.cost_base, row.total_return) == (units, 1000, 0)
        assert stored_units(session, portfolio.id)["NZX", "EXM"] == units


def test_rights_take_up_and_lapse_then_rename_history(engine):
    with Session(engine) as session, session.begin():
        portfolio, instrument = seed(session)
        rights = Instrument(
            exchange="NZX", code="EXMRG", name="Example rights", currency="NZD", kind="rights"
        )
        session.add(rights)
        session.flush()
        record_action(
            session, rights, EX, "rights_entitlement", "entitlement", {"quantity": "10"}, portfolio
        )
        session.flush()
        record_action(
            session,
            rights,
            PAID,
            "rights_take_up",
            "exercise",
            {"quantity": "6", "target": "NZX:EXM", "price": "2"},
            portfolio,
        )
        session.flush()
        record_action(session, rights, PAID, "rights_lapse", "lapse", {"quantity": "4"}, portfolio)
        session.flush()
        assert instrument_position(session, portfolio.id, rights.id, PAID).units == 0
        holding = instrument_position(session, portfolio.id, instrument.id, PAID)
        assert (holding.units, holding.cost_base) == (106, 1012)
        record_action(
            session, instrument, PAID, "rename", "rename", {"code": "EXNEW", "name": "New Example"}
        )
        record_action(session, instrument, PAID, "delisting", "delist", {})
        assert instrument.code == "EXNEW"
        assert '"old_code": "EXM"' in session.scalar(
            select(CorporateAction.details).where(CorporateAction.kind == "rename")
        )
        assert session.scalar(select(func.count()).select_from(Trade)) == 5


def test_cli_gross_up_is_opt_in_and_confirmation_requires_actual_amounts():
    parser = build_parser()
    assert parser.parse_args(["returns", "--portfolio", "Fictional"]).gross_up is False
    with pytest.raises(SystemExit):
        parser.parse_args(["dividends", "confirm", "1"])


def test_yahoo_dividend_currency_dates_and_decimals():
    class Fetcher:
        def get(self, url, params):
            assert params["events"] == "div"
            return {
                "chart": {
                    "result": [
                        {
                            "meta": {"currency": "NZD", "exchangeTimezoneName": "Pacific/Auckland"},
                            "events": {
                                "dividends": {"1": {"date": 1717200000, "amount": D("0.123456")}}
                            },
                        }
                    ]
                }
            }

    events = YahooSource(Fetcher()).dividends("EXM.NZ", EX, PAID, "NZD")
    assert events == {EX: D("0.123456")}


def test_registry_split_survives_yahoo_refresh_and_price_check_accepts_it(engine):
    from datetime import UTC, datetime

    from portfolio_digest.prices.fetch import _save_splits, held_instruments
    from portfolio_digest.prices.quality import check_data

    with Session(engine) as session, session.begin():
        portfolio, instrument = seed(session)
        record_action(session, instrument, EX, "split", "split", {"before": "1", "after": "2"})
        session.flush()
        _save_splits(session, instrument.id, {}, "yahoo", START)
        _save_splits(session, instrument.id, {EX: (D(1), D(3))}, "yahoo", START)
        split = session.get(Split, (instrument.id, EX))
        session.refresh(split)
        assert (split.shares_before, split.shares_after, split.source) == (1, 2, "registry")
        session.add(
            Price(
                instrument_id=instrument.id,
                price_date=date(2024, 5, 31),
                close=D(10),
                source="fixture",
            )
        )
        session.get(Price, (instrument.id, PAID)).close = D(4)
        session.flush()
        findings = check_data(
            session, held_instruments(session), "NZD", datetime(2024, 6, 21, tzinfo=UTC)
        )
        assert not [finding for finding in findings if finding.kind == "suspicious-jump"]
        # Selling the original number of units still leaves a holding after a split.
        session.add(
            Trade(
                portfolio_id=portfolio.id,
                instrument_id=instrument.id,
                trade_date=PAID,
                trade_type="sell",
                quantity=D(100),
                price=D(4),
                currency="NZD",
                source="fixture",
                external_ref="sell",
            )
        )
        session.flush()
        assert held_instruments(session)[0].still_held


@pytest.mark.parametrize(
    "exchange,currency,net,tax,franking",
    [
        ("NASDAQ", "USD", "85", "15", "0"),
        ("ASX", "AUD", "100", "0", "42.857143"),
    ],
)
def test_foreign_returns_use_actual_withholding_and_never_claim_franking(
    engine, exchange, currency, net, tax, franking
):
    from portfolio_digest.models import FxRate

    with Session(engine) as session, session.begin():
        portfolio, instrument = seed(session)
        instrument.exchange, instrument.currency = exchange, currency
        income = parse_income(
            "fixture",
            {
                "date": PAID.isoformat(),
                "gross": "100",
                "net": net,
                "foreign_tax": tax,
                "franking": franking,
            },
            exchange,
            instrument.code,
            currency,
            "foreign-income",
        )
        store_income(session, portfolio.id, instrument.id, income, "broker")
        session.add(
            FxRate(
                rate_date=PAID,
                currency=currency,
                base_currency="NZD",
                rate=D("1.5"),
                source="fixture",
            )
        )
        session.flush()
        row = report(session, portfolio, PAID, gross_up=True)[0]
        assert row.dividends == D(net) * D("1.5")
        assert row.gross_dividends == 150
        assert row.withholding_tax == D(tax) * D("1.5")
        assert row.imputation_gross_up == 0


def test_reimport_old_code_after_rename_keeps_same_instrument(engine):
    from portfolio_digest.importing import find_or_create_instrument

    with Session(engine) as session, session.begin():
        _, instrument = seed(session)
        record_action(session, instrument, EX, "rename", "rename", {"code": "NEW"})
        assert find_or_create_instrument(session, "NZX", "EXM", "NZD").id == instrument.id


def test_returns_carry_price_across_split_without_creating_gain(engine):
    with Session(engine) as session, session.begin():
        portfolio, instrument = seed(session)
        session.delete(session.get(Price, (instrument.id, PAID)))
        session.add(
            Price(instrument_id=instrument.id, price_date=START, close=D(10), source="fixture")
        )
        record_action(session, instrument, EX, "split", "split", {"before": "1", "after": "2"})
        assert report(session, portfolio, PAID)[0].total_return == 0


def test_confirmation_links_existing_broker_income_without_mutating_import_fields(engine):
    with Session(engine) as session, session.begin():
        portfolio, instrument = seed(session)
        item = suggest(session, portfolio, instrument, {EX: D(1)})[0]
        actual = replace(parsed(), drp_quantity=None, ex_date=None)
        income, _ = store_income(session, portfolio.id, instrument.id, actual, "broker")
        decide(session, item.id, confirmed=replace(actual, ex_date=EX), evidence="broker")
        session.flush()
        assert item.income_id == income.id
        assert session.scalar(select(func.count()).select_from(Income)) == 1
        assert store_income(session, portfolio.id, instrument.id, actual, "broker")[1] is False


def test_undo_linked_broker_import_returns_suggestion_to_pending(engine):
    with Session(engine) as session:
        portfolio, instrument = seed(session)
        item = suggest(session, portfolio, instrument, {EX: D(1)})[0]
        session.commit()
        outcome = import_file(
            session, "asb-dividend", FIXTURES / "asb-dividend.txt", portfolio.name
        )
        decide(session, item.id, confirmed=parsed(), evidence="registry")
        session.commit()
        assert undo_import(session, outcome.run_id) == 2
        session.refresh(item)
        assert (item.status, item.income_id) == ("pending", None)


def test_income_without_broker_id_has_stable_reference_across_formatting():
    content = (FIXTURES / "sharesies-income.csv").read_bytes().replace(b"fictional-nz,", b",")
    importer = SharesiesImporter()
    first = importer.parse(content, ParseContext()).incomes[0]
    second = importer.parse(
        content.replace(b",100,10,10,", b",100.00,10.0,10.00,"), ParseContext()
    ).incomes[0]
    assert first.external_ref == second.external_ref
