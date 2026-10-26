"""Dashboard contracts, with made-up data and a portable test-only schema."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from portfolio_digest.models import (
    Base,
    FxRate,
    ImportRun,
    Income,
    Instrument,
    Portfolio,
    Price,
    Trade,
)
from portfolio_digest.returns import ReturnError
from portfolio_digest.returns_loader import load_returns
from portfolio_digest.web.app import create_app
from portfolio_digest.web.binding import private_host
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


@pytest.fixture(params=["sqlite", "postgres"])
def engine_web(request):
    engine = sample_engine(
        request.getfixturevalue("engine") if request.param == "postgres" else None
    )
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine_web):
    with TestClient(create_app(engine_web, PASSWORD)) as client:
        yield client


def token(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


def sign_in(client):
    csrf = token(client.get("/login"))
    response = client.post("/login", data={"csrf": csrf, "password": PASSWORD})
    assert response.status_code == 200
    return token(response)


@pytest.mark.parametrize(
    "path,text",
    [
        ("/", "Kōwhai Fund"),
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
    response = client.get(path)
    assert response.status_code == 200
    assert text in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "unsafe-inline" not in response.headers["content-security-policy"]
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
        with TestClient(create_app(engine_web, PASSWORD)) as client:
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


def test_holding_detail_income_matches_engine(engine_web):
    with Session(engine_web) as session:
        expected = load_returns(
            session, "Kōwhai Fund", "all", datetime.now(ZoneInfo("Pacific/Auckland")).date()
        )
        result = next(row for row in expected.holdings if row.label == "NZX:KOW")
    with TestClient(create_app(engine_web, PASSWORD)) as client:
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
    with TestClient(create_app(engine_web, PASSWORD)) as client:
        overview = client.get("/p/2?period=all")
        detail = client.get("/p/2/holding/1")
    assert overview.context["report"] == expected
    assert detail.context["result"] == expected.holdings[0]
    assert expected.total.amounts.dividends == Decimal("40")


def test_missing_inputs_are_explained(engine_web):
    with Session(engine_web) as session:
        session.delete(session.get(FxRate, (DAY, "USD", "NZD")))
        session.delete(session.get(FxRate, (DAY - timedelta(days=5), "USD", "NZD")))
        session.commit()
    with TestClient(create_app(engine_web, PASSWORD)) as client:
        response = client.get(f"/p/1?period=custom&start={DAY}&end={DAY}")
    assert response.status_code == 200
    assert response.context["report"] is None
    assert "Missing USD/NZD FX" in response.text
    assert "Figures unavailable" in response.text


def test_loader_error_does_not_fabricate_zero_chart(engine_web):
    with Session(engine_web) as session:
        session.get(Trade, 2).currency = "NZD"
        session.commit()
    with TestClient(create_app(engine_web, PASSWORD)) as client:
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
    assert client.get("/p/1/trade").status_code == 401


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
    assert client.get("/p/1/trade").status_code == 401


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
        create_app(engine_web, PASSWORD, secure_cookie=True), base_url="https://testserver"
    ) as secure_client:
        assert "Secure" in secure_client.get("/").headers["set-cookie"]


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
        with TestClient(create_app(empty, PASSWORD)) as empty_client:
            response = empty_client.get("/")
            assert response.status_code == 200
            assert "No portfolios yet" in response.text
    finally:
        empty.dispose()


def test_stored_text_is_escaped(client, engine_web):
    with Session(engine_web) as session:
        session.get(Instrument, 1).name = '<script>alert("private")</script>'
        session.commit()
    response = client.get("/p/1")
    assert '<script>alert("private")</script>' not in response.text
    assert "&lt;script&gt;" in response.text


def test_session_capacity_returns_service_unavailable(client):
    from portfolio_digest.web.security import BrowserSession

    security = client.app.state.security
    security.sessions = {str(n): BrowserSession("fake", 10**12) for n in range(1024)}
    response = client.get("/login")
    assert response.status_code == 503
    assert response.headers["x-frame-options"] == "DENY"
