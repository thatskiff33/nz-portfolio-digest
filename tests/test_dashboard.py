"""Dashboard contracts, with made-up data and a portable test-only schema."""

import re
from datetime import date, timedelta
from decimal import Decimal

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
from portfolio_digest.web.app import create_app
from portfolio_digest.web.binding import private_host
from portfolio_digest.web.viewmodel import Valuations, period_dates

PASSWORD = "fictional-test-password-only"  # noqa: S105
DAY = date(2026, 10, 20)


def sample_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Production constraints use PostgreSQL regex operators. Only this disposable schema
    # removes CHECKs; page tests exercise real SQLAlchemy persistence, not mocked queries.
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copy = table.to_metadata(metadata)
        for constraint in list(copy.constraints):
            if isinstance(constraint, CheckConstraint):
                copy.constraints.remove(constraint)
    metadata.create_all(engine)
    with Session(engine) as session:
        p = Portfolio(id=1, name="Kōwhai Fund")
        q = Portfolio(id=2, name="Harbour Fund")
        nz = Instrument(id=1, exchange="NZX", code="KOW", name="Kōwhai Industries", currency="NZD")
        us = Instrument(id=2, exchange="NYSE", code="FERN", name="Fern Labs", currency="USD")
        session.add_all([p, q, nz, us])
        session.flush()
        session.add(
            ImportRun(
                id=1,
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
                    id=1,
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
                    id=2,
                    portfolio_id=1,
                    instrument_id=2,
                    trade_date=DAY - timedelta(days=5),
                    trade_type="buy",
                    quantity=Decimal("10"),
                    price=Decimal("20"),
                    currency="USD",
                    source="test",
                    external_ref="fake-2",
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
                    source="test",
                    external_ref="fake-income",
                ),
            ]
        )
        session.commit()
    return engine


@pytest.fixture
def engine_web():
    engine = sample_engine()
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


def test_decimal_valuation_and_missing_data(engine_web):
    with Session(engine_web) as session:
        p = session.get(Portfolio, 1)
        model = Valuations(session, p, DAY)
        assert model.at(DAY).value == Decimal("100.125") * Decimal("5.25") + Decimal(
            "10"
        ) * Decimal("22.5") * Decimal("1.6")
        assert model.at(DAY - timedelta(days=1)).value is None
        assert model.at(DAY - timedelta(days=11)).value == 0
        assert model.series(DAY, DAY) == [(DAY, Decimal("885.65625"))]
        # A sale reduces units; adjustments are the only source of split units.
        session.add(
            Trade(
                portfolio_id=1,
                instrument_id=1,
                trade_date=DAY,
                trade_type="sell",
                quantity=Decimal("10"),
                price=Decimal("5"),
                currency="NZD",
                source="manual",
                external_ref="fake-sell",
            )
        )
        session.flush()
        assert Valuations(session, p, DAY).at(DAY).value == Decimal("833.15625")
        fx = session.get(FxRate, (DAY, "USD", "NZD"))
        session.delete(fx)
        session.flush()
        assert Valuations(session, p, DAY).at(DAY).value is None


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
    assert period_dates("month", DAY, DAY)[0] == DAY - timedelta(days=30)
    assert period_dates("ytd", DAY, DAY)[0] == date(2026, 1, 1)
    assert period_dates("all", DAY, date(2020, 1, 1))[0] == date(2020, 1, 1)


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
