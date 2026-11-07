"""Dashboard contracts, with made-up data and a portable test-only schema."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from portfolio_digest.config import Settings
from portfolio_digest.formatting import display_quantity
from portfolio_digest.models import (
    Base,
    FxRate,
    ImportRun,
    Income,
    Instrument,
    Portfolio,
    Price,
    Split,
    Trade,
)
from portfolio_digest.reports import build_report
from portfolio_digest.reports import render as render_email
from portfolio_digest.returns import ReturnError, instrument_position
from portfolio_digest.returns_loader import load_returns
from portfolio_digest.returns_table import render_table
from portfolio_digest.web.app import create_app
from portfolio_digest.web.binding import private_host
from portfolio_digest.web.security import COOKIE
from portfolio_digest.web.viewmodel import Dashboard, period_dates

PASSWORD = "fictional-test-password-only"  # noqa: S105
DAY = date(2026, 10, 20)


def sample_engine(engine=None):
    if engine is None:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        # Only the portable schema removes PostgreSQL-specific CHECK expressions.
        metadata = MetaData()
        for table in Base.metadata.sorted_tables:
            copy = table.to_metadata(metadata)
            for constraint in list(copy.constraints):
                if isinstance(constraint, CheckConstraint):
                    copy.constraints.remove(constraint)
        metadata.create_all(engine)
    with Session(engine) as session:
        p = Portfolio(name="Kōwhai Fund")
        q = Portfolio(name="Harbour Fund")
        nz = Instrument(exchange="NZX", code="KOW", name="Kōwhai Industries", currency="NZD")
        us = Instrument(exchange="NYSE", code="FERN", name="Fern Labs", currency="USD")
        session.add_all([p, q, nz, us])
        session.flush()
        session.add(
            ImportRun(
                portfolio_id=1,
                source="test",
                status="succeeded",
                rows_imported=1,
                notes="0 duplicates; all rows accepted.",
            )
        )
        session.flush()
        session.add_all(
            [
                Trade(
                    portfolio_id=1,
                    instrument_id=1,
                    trade_date=DAY - timedelta(days=10),
                    trade_type="buy",
                    quantity=Decimal("100.125"),
                    price=Decimal("4"),
                    currency="NZD",
                    fees=Decimal("1"),
                    source="test",
                    external_ref="fake-1",
                    import_run_id=1,
                ),
                Trade(
                    portfolio_id=1,
                    instrument_id=2,
                    trade_date=DAY - timedelta(days=5),
                    trade_type="buy",
                    quantity=Decimal("10"),
                    price=Decimal("20"),
                    fx_rate=Decimal("1.5"),
                    currency="USD",
                    source="test",
                    external_ref="fake-2",
                ),
                Price(
                    instrument_id=1,
                    price_date=DAY - timedelta(days=10),
                    close=Decimal("4"),
                    source="test",
                ),
                Price(
                    instrument_id=2,
                    price_date=DAY - timedelta(days=5),
                    close=Decimal("20"),
                    source="test",
                ),
                FxRate(
                    rate_date=DAY - timedelta(days=5),
                    currency="USD",
                    base_currency="NZD",
                    rate=Decimal("1.5"),
                    source="test",
                ),
                Price(instrument_id=1, price_date=DAY, close=Decimal("5.25"), source="test"),
                Price(instrument_id=2, price_date=DAY, close=Decimal("22.5"), source="test"),
                FxRate(
                    rate_date=DAY,
                    currency="USD",
                    base_currency="NZD",
                    rate=Decimal("1.6"),
                    source="test",
                ),
                Income(
                    portfolio_id=1,
                    instrument_id=1,
                    paid_date=DAY,
                    currency="NZD",
                    kind="dividend",
                    gross_amount=Decimal("10"),
                    net_amount=Decimal("7"),
                    withholding_tax=Decimal("3"),
                    imputation_credits=Decimal("2.8"),
                    source="test",
                    external_ref="fake-income",
                ),
            ]
        )
        session.commit()
    return engine


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.database)])
def engine_web(request):
    engine = sample_engine(
        request.getfixturevalue("engine") if request.param == "postgres" else None
    )
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine_web):
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        yield client


def token(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def sign_in(client):
    csrf = token(client.get("/login"))
    response = client.post("/login", data={"csrf": csrf, "password": PASSWORD})
    assert response.status_code == 200
    return token(response)


def test_anonymous_healthz_redirects_without_session_or_database():
    engine = create_engine("sqlite://")

    def refuse_connection(*_args, **_kwargs):
        raise AssertionError("health check touched the database")

    engine.connect = refuse_connection
    app = create_app(engine, PASSWORD)
    with TestClient(app, base_url="http://localhost") as client:
        client.cookies.set(COOKIE, "not-a-session")
        response = client.get("/healthz", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fhealthz"
    assert COOKIE not in response.cookies
    assert app.state.security.sessions == {}


@pytest.mark.parametrize(
    "path,text",
    [
        ("/", "Harbour Fund"),
        ("/p/1?period=custom&start=2026-10-20&end=2026-10-20", "885.66"),
        ("/p/1?sort=value&direction=desc", "Holdings"),
        ("/p/1/holding/1", "Kōwhai Industries"),
        ("/p/1/holding/2", "Fern Labs"),
        ("/p/1/imports", "0 duplicates"),
        ("/p/1/reconcile", "Reconciliation"),
        ("/p/2", "No holdings"),
        ("/login", "App password"),
    ],
)
def test_read_pages(client, path, text):
    sign_in(client)
    response = client.get(path)
    assert response.status_code == 200
    assert text in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    if path == "/login":
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "period", ["week", "month", "ytd", "all", "2026-10-15:2026-10-20", "2026-10-20:2026-10-20"]
)
def test_dashboard_equals_returns_engine(engine_web, period):
    with Session(engine_web) as session:
        p = session.get(Portfolio, 1)
        expected = load_returns(session, p.name, period, DAY)
        dashboard = Dashboard(session, p, expected.start, expected.end)
        assert dashboard.report == expected
        assert dashboard.error is None
        assert {r.result.label: r.value for r in dashboard.holdings} == {
            r.label: r.closing for r in expected.holdings
        }
        for day, value in dashboard.series(expected.start, expected.end):
            try:
                point = load_returns(session, p.name, f"{day}:{day}", DAY)
            except ReturnError:
                assert value is None
            else:
                assert value == point.total.closing
        with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
            sign_in(client)
            response = client.get(f"/p/1?period=custom&start={expected.start}&end={expected.end}")
        assert response.status_code == 200
        context = response.context
        assert context["report"] == expected
        assert context["subtotals"] == {r.label: r for r in expected.markets}
        for result in (*expected.holdings, *expected.markets, expected.total):
            for field in ("capital", "dividends", "currency", "total"):
                assert f"{getattr(result.amounts, field):,.2f}" in response.text
                if result.percentages:
                    assert f"{getattr(result.percentages, field):,.2f}%" in response.text
            for field in ("gross_dividends", "withholding_tax", "imputation_gross_up"):
                assert f"{getattr(result, field):,.2f}" in response.text
        assert "Returns engine has not landed" not in response.text


def test_dashboard_counts_transfer_in_towards_denominator(engine_web):
    """Invented QS-023 scenario: NZ$1,000 portfolio receives a NZ$100,000 transfer in."""
    with Session(engine_web) as session:
        portfolio = Portfolio(name="Transfer Fund")
        keep = Instrument(exchange="NZX", code="KEEP", name="Keep", currency="NZD")
        moved = Instrument(exchange="NZX", code="MOVED", name="Moved", currency="NZD")
        session.add_all([portfolio, keep, moved])
        session.flush()
        for instrument, day, kind, quantity in (
            (keep, date(2026, 10, 1), "buy", 100),
            (moved, date(2026, 10, 15), "adjustment", 10000),
        ):
            session.add(
                Trade(
                    portfolio_id=portfolio.id,
                    instrument_id=instrument.id,
                    trade_date=day,
                    trade_type=kind,
                    quantity=Decimal(quantity),
                    price=Decimal(10),
                    currency="NZD",
                    fees=Decimal(0),
                    source="test",
                    external_ref=f"transfer-{instrument.code}",
                )
            )
        for instrument, day, close in (
            (keep, date(2026, 10, 12), 10),
            (keep, date(2026, 10, 19), 10),
            (moved, date(2026, 10, 12), 10),
            (moved, date(2026, 10, 19), 11),
        ):
            session.add(
                Price(
                    instrument_id=instrument.id,
                    price_date=day,
                    close=Decimal(close),
                    source="test",
                )
            )
        session.commit()
        pid = portfolio.id
        expected = load_returns(session, "Transfer Fund", "2026-10-12:2026-10-19", DAY)
    rows = {r.label: r for r in expected.holdings}
    assert rows["NZX:MOVED"].percentages.total == Decimal(10)
    assert expected.markets[0].denominator == expected.total.denominator == Decimal(101000)
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        response = client.get(f"/p/{pid}?period=custom&start=2026-10-12&end=2026-10-19")
    assert response.status_code == 200
    assert response.context["report"] == expected
    assert "10.00%" in response.text
    assert "9.90%" in response.text
    for result in (*expected.holdings, *expected.markets, expected.total):
        assert f"{result.percentages.total:,.2f}%" in response.text


def test_holding_detail_income_matches_engine(engine_web):
    with Session(engine_web) as session:
        expected = load_returns(
            session, "Kōwhai Fund", "all", datetime.now(ZoneInfo("Pacific/Auckland")).date()
        )
        result = next(row for row in expected.holdings if row.label == "NZX:KOW")
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        response = client.get("/p/1/holding/1")
    assert response.status_code == 200
    assert response.context["result"] == result
    for label, amount in (
        ("Net dividends", result.amounts.dividends),
        ("Gross income", result.gross_dividends),
        ("Withholding tax", result.withholding_tax),
        ("Imputation credits", result.imputation_gross_up),
    ):
        assert label in response.text
        assert f"{amount:,.2f}" in response.text
    assert result.amounts.dividends == Decimal("7")
    assert result.gross_dividends == Decimal("10")
    assert result.withholding_tax == Decimal("3")
    assert result.imputation_gross_up == Decimal("2.8")


def test_all_time_dashboard_includes_income_without_trades(engine_web):
    with Session(engine_web) as session:
        session.add(
            Income(
                portfolio_id=2,
                instrument_id=1,
                paid_date=DAY,
                kind="dividend",
                currency="NZD",
                gross_amount=Decimal("45"),
                net_amount=Decimal("40"),
                withholding_tax=Decimal("5"),
                imputation_credits=Decimal("12"),
                source="test",
                external_ref="income-only",
            )
        )
        session.commit()
        expected = load_returns(
            session, "Harbour Fund", "all", datetime.now(ZoneInfo("Pacific/Auckland")).date()
        )
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        overview = client.get("/p/2?period=all")
        detail = client.get("/p/2/holding/1")
    assert overview.context["report"] == expected
    assert detail.context["result"] == expected.holdings[0]
    assert expected.total.amounts.dividends == Decimal("40")


@pytest.mark.parametrize("after", [Decimal("2"), Decimal("0.5")])
@pytest.mark.parametrize("recorded_adjustment", [False, True])
def test_dashboard_quantity_replays_splits(engine_web, after, recorded_adjustment):
    with Session(engine_web) as session:
        session.add(
            Split(
                instrument_id=1,
                split_date=DAY,
                shares_before=Decimal("1"),
                shares_after=after,
                source="registry",
            )
        )
        if recorded_adjustment:
            session.add(
                Trade(
                    portfolio_id=1,
                    instrument_id=1,
                    trade_date=DAY,
                    trade_type="adjustment",
                    quantity=Decimal("100.125") * (after - 1),
                    price=Decimal("0"),
                    currency="NZD",
                    source="test",
                    external_ref="split-adjustment",
                )
            )
        session.commit()
        expected = load_returns(session, "Kōwhai Fund", "all", DAY)
        units = instrument_position(session, 1, 1, DAY).units
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        response = client.get(f"/p/1?period=custom&start={expected.start}&end={DAY}")
    assert response.status_code == 200
    assert response.context["report"] == expected
    row = next(r for r in response.context["dashboard"].holdings if r.instrument.id == 1)
    assert row.quantity == units == Decimal("100.125") * after
    assert row.value == units * row.price
    assert f"<td>{display_quantity(units)}</td>" in response.text


def test_dashboard_quantity_display_stops_at_eight_places(engine_web):
    with Session(engine_web) as session:
        session.add(
            Split(
                instrument_id=1,
                split_date=DAY,
                shares_before=Decimal("7"),
                shares_after=Decimal("1"),
                source="registry",
            )
        )
        session.commit()
        expected = load_returns(session, "Kōwhai Fund", "all", DAY)
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        response = client.get(f"/p/1?period=custom&start={expected.start}&end={DAY}")
    assert response.status_code == 200
    row = next(item for item in response.context["dashboard"].holdings if item.instrument.id == 1)
    shown = display_quantity(row.quantity)
    assert shown == "14.30357143"
    assert f"<td>{shown}</td>" in response.text
    assert "e" not in shown.lower()
    # Registry splits already round units to the ledger's eight-place policy.
    assert row.quantity == Decimal("14.30357143")


def test_missing_inputs_are_explained(engine_web):
    with Session(engine_web) as session:
        session.delete(session.get(FxRate, (DAY, "USD", "NZD")))
        session.delete(session.get(FxRate, (DAY - timedelta(days=5), "USD", "NZD")))
        session.commit()
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        response = client.get(f"/p/1?period=custom&start={DAY}&end={DAY}")
    assert response.status_code == 200
    assert response.context["report"] is None
    assert "Missing USD/NZD FX" in response.text
    assert "Figures unavailable" in response.text


def test_loader_error_does_not_fabricate_zero_chart(engine_web):
    with Session(engine_web) as session:
        session.get(Trade, 2).currency = "NZD"
        session.commit()
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        response = client.get(f"/p/1?period=custom&start={DAY}&end={DAY}")
    assert response.status_code == 200
    assert "Trade currency differs" in response.text
    assert response.context["report"] is None
    assert response.context["series"] == [(DAY, None)]


def test_sold_holding_keeps_realised_returns(engine_web):
    with Session(engine_web) as session:
        session.add(
            Trade(
                portfolio_id=1,
                instrument_id=2,
                trade_date=DAY,
                trade_type="sell",
                quantity=Decimal("10"),
                price=Decimal("22.5"),
                currency="USD",
                fx_rate=Decimal("1.6"),
                fees=Decimal("1"),
                source="test",
                external_ref="fake-sale",
            )
        )
        session.commit()
        expected = load_returns(session, "Kōwhai Fund", "all", DAY)
        dashboard = Dashboard(session, session.get(Portfolio, 1), expected.start, expected.end)
        assert dashboard.report == expected
        sold = next(row for row in dashboard.holdings if row.instrument.code == "FERN")
        assert sold.quantity == Decimal(0)
        assert sold.value == Decimal(0)
        assert sold.result.amounts.total == Decimal("58.4")


@pytest.mark.parametrize("path", ["/p/1/trade", "/p/1/imports/1/undo", "/p/1/reconcile"])
def test_writes_require_login_and_csrf(client, path):
    csrf = token(client.get("/login"))
    assert client.post(path, data={"csrf": csrf}).status_code == 401
    sign_in(client)
    assert client.post(path, data={}).status_code == 403
    assert client.post(path, data={"csrf": "invalid"}).status_code == 403


def test_trade_reconciliation_undo_and_logout(client, engine_web):
    csrf = sign_in(client)
    assert client.get("/p/1/trade").status_code == 200
    data = {
        "csrf": csrf,
        "exchange": "NZX",
        "code": "KOW",
        "trade_date": DAY.isoformat(),
        "trade_type": "adjustment",
        "quantity": "-0.125",
        "price": "0",
        "fees": "0",
        "notes": "Correct fictional opening quantity",
    }
    assert client.post("/p/1/trade", data=data).status_code == 200
    with Session(engine_web) as session:
        correction = session.scalar(select(Trade).where(Trade.source == "manual"))
        assert correction.quantity == Decimal("-0.125")
        assert correction.trade_type == "adjustment"
    response = client.post(
        "/p/1/reconcile",
        data={"csrf": csrf, "snapshot": "Exchange,Code,Units\nNZX,KOW,100\nNYSE,FERN,10\n"},
    )
    assert "All holdings match" in response.text
    response = client.post(
        "/p/1/reconcile", data={"csrf": csrf, "snapshot": "Exchange,Code,Units\nNZX,KOW,90\n"}
    )
    assert "Variance" in response.text and "10.00000000" in response.text
    assert client.post("/p/2/imports/1/undo", data={"csrf": csrf}).status_code == 404
    assert client.post("/p/1/imports/1/undo", data={"csrf": csrf}).status_code == 200
    with Session(engine_web) as session:
        assert session.get(Trade, 1) is None
        assert session.get(ImportRun, 1).status == "undone"
    assert client.post("/logout", data={"csrf": csrf}).status_code == 200
    assert client.get("/p/1/trade", follow_redirects=False).status_code == 303


def test_password_never_echoed_and_throttled(client, caplog):
    csrf = token(client.get("/login"))
    for _ in range(5):
        response = client.post("/login", data={"csrf": csrf, "password": PASSWORD + "-wrong"})
        assert response.status_code == 401
        assert PASSWORD not in response.text
    response = client.post("/login", data={"csrf": csrf, "password": PASSWORD})
    assert response.status_code == 429
    assert PASSWORD not in response.text
    assert PASSWORD not in caplog.text


def test_rotated_session_csrf_and_expiry(client):
    response = client.get("/login")
    old_csrf = token(response)
    old_cookie = client.cookies.get("portfolio_session")
    csrf = sign_in(client)
    assert csrf != old_csrf
    assert client.cookies.get("portfolio_session") != old_cookie
    assert client.post("/logout", data={"csrf": old_csrf}).status_code == 403
    security = client.app.state.security
    security.sessions[client.cookies.get("portfolio_session")].expires = 0
    assert client.get("/p/1/trade", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "path,status",
    [
        ("/p/9", 404),
        ("/p/2/holding/1", 404),
        ("/p/1?period=bad", 400),
        ("/p/1?period=custom&start=2026-10-20&end=2026-01-01", 400),
        ("/p/1?period=custom&start=bad&end=bad", 400),
        ("/p/1?sort=bad", 400),
    ],
)
def test_invalid_queries(client, path, status):
    sign_in(client)
    assert client.get(path).status_code == status


def test_invalid_trade_and_snapshot(client, engine_web):
    csrf = sign_in(client)
    for quantity in ("NaN", "1e99", "0", "-1"):
        response = client.post(
            "/p/1/trade",
            data={
                "csrf": csrf,
                "exchange": "NZX",
                "code": "KOW",
                "trade_date": "2026-10-20",
                "trade_type": "buy",
                "quantity": quantity,
                "price": "2",
                "fees": "0",
            },
        )
        assert response.status_code == 400
    assert (
        client.post("/p/1/reconcile", data={"csrf": csrf, "snapshot": "not csv"}).status_code == 400
    )
    with Session(engine_web) as session:
        assert len(list(session.scalars(select(Trade)))) == 2


def test_periods():
    assert period_dates("week", DAY, DAY)[0] == DAY - timedelta(days=7)
    assert period_dates("month", DAY, DAY)[0] == date(2026, 9, 20)
    assert period_dates("ytd", DAY, DAY)[0] == date(2025, 12, 31)
    assert period_dates("all", DAY, date(2020, 1, 1))[0] == date(2019, 12, 31)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "100.100.20.30", "fd7a:115c:a1e0::1234"])
def test_private_binding(host):
    assert private_host(host) == host


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "1.2.3.4", "192.168.1.3", "localhost", "100.128.0.1"],  # noqa: S104
)
def test_refuse_public_binding(host):
    with pytest.raises(ValueError, match="DASHBOARD_BIND"):
        private_host(host)


def test_login_csrf_and_form_size(client):
    assert client.post("/login", data={"password": PASSWORD}).status_code == 403
    assert client.post("/login", data={"csrf": "非ASCII", "password": PASSWORD}).status_code == 403
    assert client.post("/login", content=b"x" * 65537).status_code == 413


def test_secure_cookie_option(engine_web):
    with TestClient(
        create_app(engine_web, PASSWORD, secure_cookie=True), base_url="https://localhost"
    ) as secure_client:
        response = secure_client.get("/login")
        assert "Secure" in response.headers["set-cookie"]
        assert "__Host-portfolio_session" in secure_client.cookies
        sign_in(secure_client)
        assert secure_client.get("/healthz").text == "ok"
        assert "portfolio_session" not in secure_client.cookies


def test_empty_portfolio_list(engine_web):
    with Session(engine_web) as session:
        # Keep the original portfolios but make a separate empty schema for onboarding.
        assert session.get(Portfolio, 1) is not None
    empty = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata = MetaData()
    copy = Portfolio.__table__.to_metadata(metadata)
    for constraint in list(copy.constraints):
        if isinstance(constraint, CheckConstraint):
            copy.constraints.remove(constraint)
    metadata.create_all(empty)
    try:
        with TestClient(create_app(empty, PASSWORD), base_url="http://localhost") as empty_client:
            sign_in(empty_client)
            response = empty_client.get("/")
            assert response.status_code == 200
            assert "No portfolios yet" in response.text
    finally:
        empty.dispose()


def test_stored_text_is_escaped(client, engine_web):
    with Session(engine_web) as session:
        session.get(Instrument, 1).name = '<script>alert("private")</script>'
        session.commit()
    sign_in(client)
    response = client.get("/p/1")
    assert '<script>alert("private")</script>' not in response.text
    assert "&lt;script&gt;" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/p/1",
        "/p/1/holding/1",
        "/p/1/imports",
        "/p/1/reconcile",
        "/p/1/trade",
        "/healthz",
        "/missing",
        "/logout",
    ],
)
def test_every_page_requires_sign_in(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert not client.app.state.security.sessions
    assert "set-cookie" not in response.headers


def test_anonymous_capacity_preserves_authenticated_sessions(client):
    sign_in(client)
    authenticated_id = client.cookies.get(COOKIE)
    for _ in range(1100):
        client.cookies.clear()
        assert client.get("/login").status_code == 200
    security = client.app.state.security
    assert authenticated_id in security.sessions
    assert sum(not v.authenticated for v in security.sessions.values()) == 1024
    sign_in(client)


def test_anonymous_session_expires_after_ten_minutes(client, monkeypatch):
    from portfolio_digest.web import security as security_module

    now = 10000
    monkeypatch.setattr(security_module.time, "monotonic", lambda: now)
    client.get("/login")
    original = client.cookies.get(COOKIE)
    now += 599
    client.get("/login")
    assert client.cookies.get(COOKIE) == original
    now += 1
    client.get("/login")
    assert client.cookies.get(COOKIE) != original
    assert original not in client.app.state.security.sessions


def test_idle_and_absolute_expiry(client, monkeypatch):
    from portfolio_digest.web import security as security_module

    now = 10000
    monkeypatch.setattr(security_module.time, "monotonic", lambda: now)
    sign_in(client)
    for _ in range(28):
        now += 1000
        assert client.get("/healthz").status_code == 200
    now = 38800
    assert client.get("/healthz", follow_redirects=False).status_code == 303
    sign_in(client)
    now += 1799
    assert client.get("/healthz").status_code == 200
    now += 1800
    assert client.get("/healthz", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "target",
    [
        "/p/1?sort=value&direction=desc",
        "https://evil.example/",
        "//evil.example",
        "/\\evil.example",
        "/%2fevil.example",
        "/%252fevil.example",
        "/%0aevil",
    ],
)
def test_login_return_path_is_local(client, target):
    response = client.get("/login", params={"next": target})
    assert "Kōwhai Fund" not in response.text
    csrf = token(response)
    safe = target if target.startswith("/p/1?") else "/"
    assert response.context["next_path"] == safe
    response = client.post(
        "/login", data={"csrf": csrf, "password": PASSWORD, "next": target}, follow_redirects=False
    )
    assert response.headers["location"] == safe


def test_login_returns_to_requested_page(client):
    response = client.get("/p/1/imports?sort=date")
    assert response.context["next_path"] == "/p/1/imports?sort=date"
    response = client.post(
        "/login",
        data={"csrf": token(response), "password": PASSWORD, "next": response.context["next_path"]},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/p/1/imports?sort=date"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/login",
        "/logout",
        "/static/dashboard.css",
        "/healthz",
        "/p/1",
        "/p/1/holding/1",
        "/p/1/imports",
        "/p/1/reconcile",
        "/p/1/trade",
        "/missing",
    ],
)
@pytest.mark.parametrize("method", ["get", "post"])
def test_untrusted_host_is_rejected(client, path, method):
    response = getattr(client, method)(
        path, headers={"host": "evil.example"}, follow_redirects=False
    )
    assert response.status_code == 400
    assert not client.app.state.security.sessions


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:8000",
        "127.0.0.1",
        "127.0.0.1:8000",
        "100.100.20.30",
        "100.100.20.30:8000",
        "portfolio.tail.ts.net",
        "portfolio.tail.ts.net:443",
    ],
)
def test_allowed_hosts(engine_web, host):
    settings = Settings(
        database_url="sqlite://",
        dashboard_bind="100.100.20.30",
        dashboard_hostnames=" portfolio.tail.ts.net, other.tail.ts.net ",
    )
    with TestClient(
        create_app(engine_web, PASSWORD, settings=settings), base_url="http://localhost"
    ) as browser:
        assert browser.get("/login", headers={"host": host}).status_code == 200


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("yes", True),
        ("true", True),
        ("TRUE", True),
        ("0", False),
        ("no", False),
        ("false", False),
    ],
)
def test_dashboard_settings_bool(monkeypatch, value, expected):
    monkeypatch.setenv("DASHBOARD_SECURE_COOKIE", value)
    assert Settings(database_url="sqlite://").dashboard_secure_cookie is expected


def test_logout_does_not_allocate_replacement_session(client):
    csrf = sign_in(client)
    response = client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert not client.app.state.security.sessions
    assert not client.cookies.get("portfolio_session")


def test_login_throttle_is_per_address_not_cookie_or_forwarded_header(client, engine_web):
    csrf = token(client.get("/login"))
    for _ in range(5):
        assert client.post("/login", data={"csrf": csrf, "password": "wrong"}).status_code == 401
    client.cookies.clear()
    csrf = token(client.get("/login"))
    assert (
        client.post(
            "/login",
            data={"csrf": csrf, "password": PASSWORD},
            headers={"x-forwarded-for": "100.100.20.30"},
        ).status_code
        == 429
    )
    with TestClient(
        client.app, base_url="http://localhost", client=("100.100.20.30", 12345)
    ) as other:
        sign_in(other)


def test_login_global_ceiling_and_window_expiry(client, monkeypatch):
    from portfolio_digest.web import security as security_module

    now = 10000
    monkeypatch.setattr(security_module.time, "monotonic", lambda: now)
    # Oversized passwords still count, without paying the scrypt cost for this test.
    for address in range(10):
        with TestClient(
            client.app, base_url="http://localhost", client=(f"100.100.20.{address}", 12345)
        ) as other:
            csrf = token(other.get("/login"))
            for _ in range(5):
                assert (
                    other.post("/login", data={"csrf": csrf, "password": "x" * 1025}).status_code
                    == 401
                )
    csrf = token(client.get("/login"))
    assert client.post("/login", data={"csrf": csrf, "password": PASSWORD}).status_code == 429
    assert len(client.app.state.security.failures) == 50
    now += 300
    sign_in(client)
    assert not client.app.state.security.failures


def test_client_login_throttle_expires(client, monkeypatch):
    from portfolio_digest.web import security as security_module

    now = 10000
    monkeypatch.setattr(security_module.time, "monotonic", lambda: now)
    csrf = token(client.get("/login"))
    for _ in range(5):
        assert client.post("/login", data={"csrf": csrf, "password": "x" * 1025}).status_code == 401
    now += 299
    assert client.post("/login", data={"csrf": csrf, "password": PASSWORD}).status_code == 429
    now += 1
    sign_in(client)


@pytest.mark.parametrize("password", ["", "too-short"])
def test_dashboard_refuses_missing_or_short_password(engine_web, password):
    with pytest.raises(ValueError, match="at least 16"):
        create_app(engine_web, password)


def test_overview_series_values_match_linear_lookup(engine_web, monkeypatch):
    from portfolio_digest import parcels, returns

    with Session(engine_web) as session:
        dashboard = Dashboard(session, session.get(Portfolio, 1), DAY - timedelta(days=10), DAY)
        actual = dashboard.series(DAY - timedelta(days=10), DAY)

        def linear_lookup(points, day, label, flags, calendar, *, as_of=None):
            point = max((p for p in points if p.day <= day), key=lambda p: p.day, default=None)
            if point is None:
                raise ReturnError(f"Missing {label} on or before {day}.")
            if point.stale or parcels._skips_trading_day(calendar, point.day, day, as_of=as_of):
                flags.add(f"Carried forward {label} for {day} (source {point.day})")
            return point.value

        monkeypatch.setattr(returns, "lookup", linear_lookup)
        assert dashboard.series(DAY - timedelta(days=10), DAY) == actual


def test_holding_bounds_chart_and_table_and_ignores_unrelated_bad_inputs(engine_web):
    with Session(engine_web) as session:
        # An unrelated invalid currency must not prevent this holding being valued.
        session.scalar(select(Trade).where(Trade.instrument_id == 2)).currency = "AUD"
        first = DAY - timedelta(days=500)
        session.add_all(
            Price(
                instrument_id=1,
                price_date=first + timedelta(days=n),
                close=Decimal(n) / 100,
                source="test",
            )
            for n in range(480)
        )
        session.commit()
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        response = client.get("/p/1/holding/1")
    assert response.status_code == 200
    series = response.context["series"]
    assert len(series) == 180
    assert series[0] == (first, Decimal(0))
    assert series[-1] == (DAY, Decimal("5.25"))
    assert response.context["result"].closing == Decimal("100.125") * Decimal("5.25")
    assert response.text.count("data-chart-date=") == len(series)
    assert response.context["returns_error"] is None


def test_holding_input_error_is_explained(engine_web):
    with Session(engine_web) as session:
        session.scalar(select(Trade).where(Trade.instrument_id == 1)).currency = "AUD"
        session.commit()
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        response = client.get("/p/1/holding/1")
    assert response.status_code == 200
    assert "Trade currency differs from instrument currency" in response.context["returns_error"]
    assert response.context["result"] is None


def test_holding_future_activity_does_not_invert_report_dates(engine_web):
    today = datetime.now(ZoneInfo("Pacific/Auckland")).date()
    with Session(engine_web) as session:
        session.scalar(select(Trade).where(Trade.instrument_id == 2)).trade_date = (
            today + timedelta(days=30)
        )
        session.commit()
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        response = client.get("/p/1/holding/2")
    assert response.status_code == 200
    assert response.context["start"] <= response.context["end"] == today
    assert response.context["result"] is None
    assert response.context["returns_error"] is None
    # Future ledger rows remain visible, but are excluded from valuation inputs.
    assert len(response.context["trades"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("exchange", "unknown"),
        ("code", "bad code"),
        ("trade_date", "bad"),
        ("trade_type", "unknown"),
        ("quantity", "NaN"),
        ("price", "-1"),
        ("fees", "-1"),
        ("notes", "invalid\x01note"),
    ],
)
def test_form_errors_retain_entries_and_link_fields(client, engine_web, field, value):
    csrf = sign_in(client)
    data = dict(
        csrf=csrf,
        exchange="NZX",
        code="KOW",
        trade_date=str(DAY),
        trade_type="buy",
        quantity="2",
        price="3",
        fees="0",
        notes="Made-up note",
    )
    data[field] = value
    response = client.post("/p/1/trade", data=data)
    assert response.status_code == 400
    assert response.context["values"][field] == value
    assert field in response.context["errors"]
    assert 'role="alert"' in response.text
    assert f'href="#{field}"' in response.text
    assert f'id="{field}-error"' in response.text
    assert 'aria-invalid="true"' in response.text
    assert "Made-up note" in response.text or field == "notes"
    with Session(engine_web) as session:
        assert len(list(session.scalars(select(Trade)))) == 2


def test_snapshot_validation_and_success_retain_csv(client):
    csrf = sign_in(client)
    bad = "<script>bad</script>"
    response = client.post("/p/1/reconcile", data={"csrf": csrf, "snapshot": bad})
    assert response.status_code == 400
    assert 'href="#snapshot"' in response.text
    assert "snapshot-help snapshot-error" in response.text
    assert "&lt;script&gt;bad&lt;/script&gt;" in response.text
    snapshot = "Exchange,Code,Units\nNZX,KOW,100.125\nNYSE,FERN,10"
    response = client.post("/p/1/reconcile", data={"csrf": csrf, "snapshot": snapshot})
    assert response.context["values"]["snapshot"] == snapshot
    assert response.context["status_message"] == "All holdings match the snapshot."


@pytest.mark.parametrize(
    "path,values",
    [
        ("/p/1/trade", {"code": "KOW", "quantity": "2"}),
        ("/p/1/reconcile", {"snapshot": "Exchange,Code,Units\nNZX,KOW,3"}),
    ],
)
def test_stale_csrf_recovers_authenticated_forms_without_write(client, engine_web, path, values):
    csrf = sign_in(client)
    response = client.post(path, data={"csrf": "stale", **values})
    assert response.status_code == 403
    assert "This form has expired. Check your entries and resubmit." in response.text
    assert token(response) == csrf
    for field, value in values.items():
        assert response.context["values"][field] == value
    with Session(engine_web) as session:
        assert len(list(session.scalars(select(Trade)))) == 2


def test_titles_current_navigation_and_private_error_sidebar(client):
    paths = [
        "/p/1",
        "/p/1/holding/1",
        "/p/1/imports",
        "/p/1/reconcile",
        "/p/1/trade",
        "/login",
        "/missing",
    ]
    assert "Kōwhai Fund" not in client.get("/missing").text
    sign_in(client)
    titles = []
    for path in paths:
        response = client.get(path)
        titles.append(re.search(r"<title>(.*?)</title>", response.text, re.S)[1].strip())
        if path == "/login":
            assert "Kōwhai Fund" not in response.text
        else:
            assert "Kōwhai Fund" in response.text
        if path.startswith("/p/1") and "/holding/" not in path:
            assert response.text.count('aria-current="page"') == 2
    assert len(set(titles)) == len(titles)
    error = client.get("/missing")
    assert "Error: 404" in error.text
    assert ">Sign in</a>" not in error.text


def test_save_and_undo_messages_are_consumed_once(client):
    csrf = sign_in(client)
    response = client.post(
        "/p/1/trade",
        data=dict(
            csrf=csrf,
            exchange="NZX",
            code="KOW",
            trade_date=str(DAY),
            trade_type="buy",
            quantity="2",
            price="3",
            fees="0",
        ),
    )
    assert response.context["status_message"] == "Trade saved."
    assert client.get("/p/1").context["status_message"] == ""
    response = client.post("/p/1/imports/1/undo", data={"csrf": csrf})
    assert response.context["status_message"] == "Import undone."
    assert client.get("/p/1/imports").context["status_message"] == ""


@pytest.mark.parametrize(
    "period,start,end,original_start,original_end,expected",
    [
        ("week", "2026-09-20", "2026-10-20", "2026-09-20", "2026-10-20", "week"),
        ("month", "2026-09-21", "2026-10-20", "2026-09-20", "2026-10-20", "custom"),
        ("week", "2026-09-20", "2026-10-19", "2026-09-20", "2026-10-20", "custom"),
        ("month", "2026-09-21", "2026-10-20", "", "", "custom"),
        ("month", "2026-09-20", "2026-10-20", "", "", "month"),
        ("month", "", "", "", "", "month"),
    ],
)
def test_period_date_edit_matrix(period, start, end, original_start, original_end, expected):
    from portfolio_digest.web.viewmodel import effective_period

    assert effective_period(period, DAY, DAY, start, end, original_start, original_end) == expected
    resolved = period_dates(period, DAY, DAY, start, end, original_start, original_end)
    if expected == "custom":
        assert resolved == (date.fromisoformat(start), date.fromisoformat(end))
    elif expected == "week":
        assert resolved == (DAY - timedelta(days=7), DAY)


def test_custom_period_remains_selected_when_sorting(client):
    sign_in(client)
    response = client.get(
        "/p/1",
        params={
            "period": "month",
            "start": "2026-10-01",
            "end": "2026-10-20",
            "original_start": "2026-09-20",
            "original_end": "2026-10-20",
        },
    )
    assert response.context["period"] == "custom"
    assert 'name="period" value="custom"' in response.text
    response = client.get(
        "/p/1",
        params={
            "period": "custom",
            "start": "2026-10-01",
            "end": "2026-10-20",
            "sort": "value",
        },
    )
    assert response.context["period"] == "custom"
    assert str(response.context["start"]) == "2026-10-01"


def test_persistence_error_preserves_form_without_exposing_details(client, monkeypatch):
    from portfolio_digest.importing import ImportFailure

    def fail(*args):
        raise ImportFailure("Private persistence details")

    monkeypatch.setattr("portfolio_digest.web.app.add_trade", fail)
    csrf = sign_in(client)
    response = client.post(
        "/p/1/trade",
        data=dict(
            csrf=csrf,
            exchange="NZX",
            code="KOW",
            trade_date=str(DAY),
            trade_type="buy",
            quantity="2",
            price="3",
            fees="0",
            notes="Synthetic note",
        ),
    )
    assert response.status_code == 400
    assert "Synthetic note" in response.text
    assert "Private persistence details" not in response.text


def test_missing_static_file_has_accessible_error_page(client):
    response = client.get("/static/missing.css")
    assert response.status_code == 404
    assert "Error: 404" in response.text


def test_dashboard_cli_and_email_round_the_same_value_half_up():
    """A closing value of 8.125 and a gain of 0.125 read 8.13 and 0.13 in every channel."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copy = table.to_metadata(metadata)
        for constraint in list(copy.constraints):
            if isinstance(constraint, CheckConstraint):
                copy.constraints.remove(constraint)
    metadata.create_all(engine)
    start = DAY - timedelta(days=10)
    with Session(engine) as session:
        session.add_all(
            [
                Portfolio(name="Tōtara Fund", base_currency="NZD"),
                Instrument(exchange="NZX", code="TOT", name="Tōtara Mills", currency="NZD"),
            ]
        )
        session.flush()
        session.add_all(
            [
                Trade(
                    portfolio_id=1,
                    instrument_id=1,
                    trade_date=start,
                    trade_type="buy",
                    quantity=Decimal("1"),
                    price=Decimal("8"),
                    currency="NZD",
                    fees=Decimal("0"),
                    source="test",
                    external_ref="fake-half",
                ),
                Price(instrument_id=1, price_date=start, close=Decimal("8"), source="test"),
                Price(instrument_id=1, price_date=DAY, close=Decimal("8.125"), source="test"),
            ]
        )
        session.commit()
        cli = render_table(load_returns(session, "Tōtara Fund", "all", DAY))
        email_html, email_text = render_email(build_report(session, start, DAY))
    with TestClient(create_app(engine, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        dashboard = client.get(f"/p/1?period=custom&start={start}&end={DAY}").text
    engine.dispose()
    for channel in (dashboard, cli, email_html, email_text):
        # Whole two-place numbers only, so full-precision chart data does not match.
        shown = set(re.findall(r"(?<![\d.])\d+\.\d\d(?![\d.])", channel))
        assert {"8.13", "0.13"} <= shown
        assert not {"8.12", "0.12"} & shown


@pytest.mark.parametrize("day,stale", [(21, False), (22, True)])
def test_live_routes_and_dashboard_reuse_frozen_instant(engine_web, monkeypatch, day, stale):
    from portfolio_digest.web import app as web_app
    from portfolio_digest.web import viewmodel

    instant = datetime(2026, 10, day, 9, tzinfo=ZoneInfo("Pacific/Auckland"))
    clocks = []

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            clocks.append(tz)
            return instant.astimezone(tz)

    monkeypatch.setattr(web_app, "datetime", FrozenDatetime)
    seen = []
    calculate = viewmodel.calculate_returns

    def capture(*args, **kwargs):
        seen.append(kwargs.get("as_of"))
        return calculate(*args, **kwargs)

    monkeypatch.setattr(viewmodel, "calculate_returns", capture)
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        clocks.clear()
        seen.clear()
        page = client.get("/p/1?period=all")
        detail = client.get("/p/1/holding/2")
    assert page.status_code == detail.status_code == 200
    row = next(r for r in page.context["report"].holdings if r.label == "NYSE:FERN")
    assert bool(row.flags) == stale
    assert detail.context["result"].flags == row.flags
    assert page.context["end"] == instant.date()
    assert len(clocks) == 2
    assert len(seen) > 2  # Includes sampled series calculations.
    assert all(value == instant for value in seen)


@pytest.mark.parametrize(
    "bind", ["fd7a:115c:a1e0::1234", "fd7a:115c:a1e0:0:0:0:0:1234", "FD7A:115C:A1E0::1234"]
)
@pytest.mark.parametrize(
    "host",
    [
        "[fd7a:115c:a1e0::1234]",
        "[fd7a:115c:a1e0::1234]:8000",
        "[fd7a:115c:a1e0:0:0:0:0:1234]:8000",
    ],
)
def test_allowed_ipv6_host(engine_web, host, bind):
    settings = Settings(database_url="sqlite://", dashboard_bind=bind)
    with TestClient(
        create_app(engine_web, PASSWORD, settings=settings), base_url="http://localhost"
    ) as browser:
        assert browser.get("/login", headers={"host": host}).status_code == 200
        assert (
            browser.get("/login", headers={"host": "[fd7a:115c:a1e0::9999]:8000"}).status_code
            == 400
        )


def test_authenticated_session_capacity_does_not_block_login_form(client, monkeypatch):
    from portfolio_digest.web import security as security_module
    from portfolio_digest.web.security import BrowserSession

    now = 10000
    monkeypatch.setattr(security_module.time, "monotonic", lambda: now)
    security = client.app.state.security
    security.sessions = {
        str(n): BrowserSession("fake", now + 28800, True, now) for n in range(1024)
    }
    response = client.get("/login")
    assert response.status_code == 200
    response = client.post("/login", data={"csrf": token(response), "password": PASSWORD})
    assert response.status_code == 503
    assert sum(v.authenticated for v in security.sessions.values()) == 1024


def test_dashboard_reads_settings_from_environment(engine_web, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_BIND", "100.100.20.30")
    monkeypatch.setenv("DASHBOARD_HOSTNAMES", "portfolio.tail.ts.net")
    monkeypatch.setenv("DASHBOARD_SECURE_COOKIE", "yes")
    with TestClient(create_app(engine_web), base_url="https://portfolio.tail.ts.net") as browser:
        response = browser.get("/login")
        assert response.status_code == 200
        assert "__Host-portfolio_session" in browser.cookies
        sign_in(browser)
        assert browser.get("/healthz").text == "ok"
        assert browser.get("/login", headers={"host": "100.100.20.30:8000"}).status_code == 200


def test_default_morning_fetch_leaves_no_fx_flag_on_summer_tuesday(engine_web, monkeypatch):
    from portfolio_digest.prices.calendars import FX, for_exchange
    from portfolio_digest.scheduling import read_config
    from portfolio_digest.web import app as web_app

    zone = ZoneInfo("Pacific/Auckland")
    midnight = datetime(2027, 1, 12, tzinfo=zone)
    instant = midnight.replace(hour=14)
    config = read_config(Path(__file__).parent.parent / "schedules.toml")
    fetches = [
        schedule.next_run(midnight, config.timezone)
        for schedule in config.schedules
        if schedule.kind == "prices"
    ]
    last_fetch = max(due for due in fetches if due <= instant)
    # Model a successful provider response containing only sessions closed by the fetch.
    # At the former 10:30 time, Monday's FX fix would still be unavailable.
    with Session(engine_web) as session:
        session.add_all(
            [
                Price(
                    instrument_id=1,
                    price_date=for_exchange("NZX").latest_closed_day(last_fetch),
                    close=Decimal("5.25"),
                    source="test",
                ),
                Price(
                    instrument_id=2,
                    price_date=for_exchange("NYSE").latest_closed_day(last_fetch),
                    close=Decimal("22.5"),
                    source="test",
                ),
                FxRate(
                    rate_date=FX.latest_closed_day(last_fetch),
                    currency="USD",
                    base_currency="NZD",
                    rate=Decimal("1.6"),
                    source="test",
                ),
            ]
        )
        session.commit()

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(web_app, "datetime", FrozenDatetime)
    with TestClient(create_app(engine_web, PASSWORD), base_url="http://localhost") as client:
        sign_in(client)
        page = client.get("/p/1?period=all")
    assert page.status_code == 200
    report = page.context["report"]
    assert report is not None
    foreign = next(row for row in report.holdings if row.label == "NYSE:FERN")
    assert foreign.closing == Decimal("360")
    assert foreign.flags == ()
    assert not any("FX" in flag for flag in report.total.flags)
