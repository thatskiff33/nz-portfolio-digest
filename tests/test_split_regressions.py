"""B3 regressions using invented holdings and Decimal amounts."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D, ROUND_HALF_EVEN, localcontext
from types import SimpleNamespace

from sqlalchemy import CheckConstraint, MetaData, create_engine, select
from sqlalchemy.orm import Session
from portfolio_digest.models import Base, Portfolio, Instrument, Trade, Split, Price
from portfolio_digest.actions import record_action
from portfolio_digest.importing import ImportFailure

import pytest

from portfolio_digest.returns import Holding, Observation, SplitEvent, Transaction, holding_return, position
from portfolio_digest.reconcile import compare, reconcile
from portfolio_digest.year_end import calculate_year_end

BUY, START, ACTION, END = date(2026, 1, 1), date(2026, 3, 31), date(2026, 6, 1), date(2027, 3, 31)


def fixture(before, after, automatic=True):
    with localcontext() as ctx:
        ctx.prec = 80
        target = (D(100) * D(after) / D(before)).quantize(D('.00000001'), rounding=ROUND_HALF_EVEN)
        price = D(1000) / target
    return Holding('NZX', 'FERN', 'NZD', (Transaction(BUY, 'buy', D(100), D(10)),),
                   (Observation(START, D(10)), Observation(END, price)),
                   splits=(SplitEvent(ACTION, D(before), D(after), automatic),)), target, price


@pytest.mark.parametrize('before,after', [('3','1'), ('3','2'), ('3','4'), ('7','2'), ('30','1')])
def test_registry_rounding_preserves_cost_and_allows_full_exit(before, after):
    h, target, price = fixture(before, after)
    r = holding_return(h, START, END, {})
    assert r.units == target
    assert abs(r.amounts.total) < D('1e-70')
    sold = replace(h, trades=h.trades + (Transaction(END, 'sell', target, price),))
    assert holding_return(sold, START, END, {}).units == 0
    year = calculate_year_end((sold,), (), {}, 2027)
    assert year.total.gains.quantize(D('.01')) == 0
    assert year.total.losses.quantize(D('.01')) == 0
    rows = [SimpleNamespace(trade_date=t.day, trade_type=t.kind, quantity=t.quantity, price=t.price, fees=t.fees, consideration=None) for t in h.trades]
    splits = [SimpleNamespace(split_date=ACTION, shares_before=D(before), shares_after=D(after), source='registry')]
    p = position(rows, splits, END)
    assert (p.units, p.cost_base) == (target, D(1000))


def test_reconcile_rounds_both_operands_half_even():
    assert reconcile({1: D('33.333333333333')}, {1: D('33.33333333')}) == []
    assert compare({('NZX','FERN'): D('33.333333333333')}, {('NZX','FERN'): D('33.33333333')}) == []


def test_downloaded_missing_adjustment_is_actionable_even_before_opening():
    h, _, _ = fixture('1', '4', False)
    r = holding_return(h, ACTION + timedelta(days=1), END, {})
    assert f'Split on {ACTION} not yet recorded; add the adjustment' in r.flags


def test_legacy_differently_dated_duplicate_is_flagged():
    h, _, _ = fixture('1', '2')
    h = replace(h, trades=h.trades + (Transaction(ACTION + timedelta(days=2), 'adjustment', D(100), D(0)),))
    assert any('docs/dividends.md' in f for f in holding_return(h, START, END, {}).flags)


def test_downloaded_and_registry_carried_prices_are_equal():
    h, _, _ = fixture('1', '2')
    h = replace(h, prices=(Observation(START, D(10)),))
    downloaded = replace(h, splits=(replace(h.splits[0], automatic=False),),
                         trades=h.trades + (Transaction(ACTION, 'adjustment', D(100), D(0)),))
    assert holding_return(h, START, END, {}).closing == holding_return(downloaded, START, END, {}).closing == D(1000)


@pytest.mark.parametrize('offset', [-10, 0, 10])
@pytest.mark.parametrize('registry_first', [True, False])
def test_registry_duplicate_refused_in_both_orders(split_engine, offset, registry_first):
    from datetime import timedelta
    from portfolio_digest.manual import add_trade, prepare_trade

    with Session(split_engine) as session:
        portfolio = Portfolio(name='Invented B3')
        instrument = Instrument(exchange='NZX', code='FERNB3', currency='NZD', name='Invented fern')
        session.add_all([portfolio, instrument])
        session.flush()
        add_trade(session, portfolio.name, prepare_trade(exchange='NZX', code='FERNB3', trade_date='2024-01-01', trade_type='buy', quantity='100', price='10'))
        entry = prepare_trade(exchange='NZX', code='FERNB3', trade_date=(ACTION + timedelta(days=offset)).isoformat(), trade_type='adjustment', quantity='100', price='0')
        def registry():
            record_action(session, instrument, ACTION, 'split', 'FICTION-B3', {'before': '1', 'after': '2'})
        if registry_first:
            registry()
            with pytest.raises(ImportFailure, match='docs/dividends.md'):
                add_trade(session, portfolio.name, entry)
        else:
            add_trade(session, portfolio.name, entry)
            with pytest.raises(ImportFailure, match='docs/dividends.md'):
                registry()


@pytest.fixture
def split_engine():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(metadata)
        for constraint in list(copied.constraints):
            if isinstance(constraint, CheckConstraint):
                copied.constraints.remove(constraint)
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.mark.parametrize('before,after', [('3','1'), ('3','2'), ('3','4'), ('7','2'), ('30','1')])
def test_foreign_cost_before_opening_survives_rounded_consolidation(before, after):
    h, target, price = fixture(before, after)
    with localcontext() as ctx:
        ctx.prec = 80
        sale_price = D('50005') / target
    h = replace(h, exchange='ASX', currency='AUD',
                trades=(Transaction(BUY, 'buy', D(100), D(500), D(5), D('1.1')),
                        Transaction(END, 'sell', target, sale_price, fx=D('1.1'))),
                splits=(replace(h.splits[0], day=date(2026,3,1)),),
                prices=(Observation(START, sale_price),))
    rates = {'AUD': (Observation(START, D('1.1')), Observation(END, D('1.1')))}
    year = calculate_year_end((h,), (), rates, 2027)
    assert year.foreign_peak == D('55005.50')
    assert year.fif_exceeded
    assert year.total.gains.quantize(D('.01')) == 0
    assert year.total.losses.quantize(D('.01')) == 0
    assert year.performance.total.units == 0


@pytest.mark.parametrize('units,expected', [('0.00000003', '0.00000002'), ('0.00000005', '0.00000002')])
def test_registry_unit_ties_are_half_even(units, expected):
    from portfolio_digest.returns import holding_units_at
    h, _, _ = fixture('2','1')
    h = replace(h, trades=(Transaction(BUY, 'buy', D(units), D(10)),))
    assert holding_units_at(h, END) == D(expected)


def test_multiple_parcels_keep_entry_fx_and_total_cost_after_rescaling():
    h, target, price = fixture('3', '1')
    h = replace(h, currency='AUD', trades=(
        Transaction(START + timedelta(days=1), 'buy', D(40), D(10), fx=D(2)),
        Transaction(START + timedelta(days=2), 'buy', D(60), D(10), fx=D(3)),
        Transaction(END, 'sell', target, price, fx=D(4)),
    ))
    r = holding_return(h, START, END, {})
    assert r.units == 0
    assert abs(r.amounts.capital) < D('1e-70')
    assert abs(r.amounts.currency - D(1400)) < D('1e-70')
    assert abs(r.amounts.total - (r.sales - r.purchases)) < D('1e-70')


@pytest.mark.parametrize('offset,quantity', [(-11,'100'), (11,'100'), (0,'99')])
def test_unrelated_or_outside_window_adjustments_are_allowed(split_engine, offset, quantity):
    from portfolio_digest.manual import add_trade, prepare_trade
    with Session(split_engine) as session:
        portfolio = Portfolio(name='Invented outside window')
        instrument = Instrument(exchange='NZX', code='B3', currency='NZD', name='Invented')
        session.add_all([portfolio, instrument])
        session.flush()
        add_trade(session, portfolio.name, prepare_trade(exchange='NZX', code='B3', trade_date=BUY.isoformat(), trade_type='buy', quantity='100', price='10'))
        record_action(session, instrument, ACTION, 'split', 'OUTSIDE', {'before':'1', 'after':'2'})
        add_trade(session, portfolio.name, prepare_trade(exchange='NZX', code='B3', trade_date=(ACTION + timedelta(days=offset)).isoformat(), trade_type='adjustment', quantity=quantity, price='0'))


@pytest.mark.parametrize('resolved,future,no_ownership', [(False,False,False), (True,False,False), (False,True,False), (False,False,True)])
def test_missing_downloaded_split_in_prices_check_and_cli(split_engine, monkeypatch, capsys, resolved, future, no_ownership):
    from contextlib import contextmanager
    from datetime import UTC, datetime
    from portfolio_digest import cli
    from portfolio_digest.prices.fetch import held_instruments
    from portfolio_digest.prices.quality import check_data
    with Session(split_engine) as session:
        portfolio = Portfolio(name='Invented check')
        instrument = Instrument(exchange='NZX', code='CHECK', currency='NZD', name='Invented')
        session.add_all([portfolio, instrument])
        session.flush()
        def add(day, kind, units, ref):
            session.add(Trade(portfolio_id=portfolio.id, instrument_id=instrument.id, trade_date=day, trade_type=kind, quantity=D(units), price=D(10), fees=D(0), currency='NZD', source='test', external_ref=ref))
        add(ACTION if no_ownership else BUY, 'buy', '100', 'BUY')
        if resolved:
            add(ACTION, 'adjustment', '300', 'ADJUST')
        split_day = date(2099,1,1) if future else ACTION
        session.add(Split(instrument_id=instrument.id, split_date=split_day, shares_before=D(1), shares_after=D(4), source='yahoo'))
        session.add(Price(instrument_id=instrument.id, price_date=END, close=D('2.5'), source='test', is_stale=False))
        session.flush()
        findings = check_data(session, held_instruments(session), 'NZD', datetime(2027,4,1,tzinfo=UTC))
        messages = [f.detail for f in findings if f.kind == 'split-recording']
        assert bool(messages) == (not resolved and not future and not no_ownership)
        if messages:
            assert f'Split on {ACTION} not yet recorded; add the adjustment' in messages[0]
            @contextmanager
            def scope(_):
                yield session
            monkeypatch.setattr(cli, 'make_engine', lambda: None)
            monkeypatch.setattr(cli, 'session_scope', scope)
            monkeypatch.setattr(cli, 'get_settings', lambda: SimpleNamespace(base_currency='NZD'))
            assert cli.cmd_prices_check(SimpleNamespace(instrument=None)) == 1
            assert f'Split on {ACTION} not yet recorded; add the adjustment' in capsys.readouterr().out


def test_csv_snapshot_reconciles_registry_consolidation(split_engine):
    from portfolio_digest.reconcile import parse_snapshot, stored_units
    with Session(split_engine) as session:
        portfolio = Portfolio(name='Invented CSV')
        instrument = Instrument(exchange='NZX', code='CSV', currency='NZD', name='Invented')
        session.add_all([portfolio, instrument])
        session.flush()
        session.add(Trade(portfolio_id=portfolio.id, instrument_id=instrument.id, trade_date=BUY, trade_type='buy', quantity=D(100), price=D(10), fees=D(0), currency='NZD', source='test', external_ref='CSV-BUY'))
        session.add(Split(instrument_id=instrument.id, split_date=ACTION, shares_before=D(3), shares_after=D(1), source='registry'))
        session.flush()
        assert compare(stored_units(session, portfolio.id), parse_snapshot(b'Exchange,Code,Units\nNZX,CSV,33.33333333\n')) == []
