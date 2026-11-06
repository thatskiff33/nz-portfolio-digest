"""Dashboard contracts, with made-up data and a portable test-only schema."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
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
    Split,
    Trade,
)
from portfolio_digest.returns import ReturnError, instrument_position
from portfolio_digest.returns_loader import load_returns
from portfolio_digest.web.app import create_app
from portfolio_digest.web.binding import private_host
from portfolio_digest.web.security import BrowserSession
from portfolio_digest.web.viewmodel import Dashboard, period_dates

PASSWORD = "fictional-test-password-only"  # noqa: S105
DAY = date(2026, 10, 20)
BASE_URL = "http://127.0.0.1"


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


def open_client(engine, password=PASSWORD, *, base_url=BASE_URL, client_address=None, **options):
    params = {"client": client_address} if client_address is not None else {}
    return TestClient(create_app(engine, password, **options), base_url=base_url, **params)


@pytest.fixture
def client(engine_web):
    with open_client(engine_web) as client:
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
        with open_client(engine_web) as client:
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


def test_holding_detail_income_matches_engine(engine_web):
    with Session(engine_web) as session:
        expected = load_returns(
            session, "Kōwhai Fund", "all", datetime.now(ZoneInfo("Pacific/Auckland")).date()
        )
        result = next(row for row in expected.holdings if row.label == "NZX:KOW")
    with open_client(engine_web) as client:
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
    with open_client(engine_web) as client:
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
    with open_client(engine_web) as client:
        sign_in(client)
        response = client.get(f"/p/1?period=custom&start={expected.start}&end={DAY}")
    assert response.status_code == 200
    assert response.context["report"] == expected
    row = next(r for r in response.context["dashboard"].holdings if r.instrument.id == 1)
    assert row.quantity == units == Decimal("100.125") * after
    assert row.value == units * row.price
    assert f"<td>{units.normalize():f}</td>" in response.text


def test_missing_inputs_are_explained(engine_web):
    with Session(engine_web) as session:
        session.delete(session.get(FxRate, (DAY, "USD", "NZD")))
        session.delete(session.get(FxRate, (DAY - timedelta(days=5), "USD", "NZD")))
        session.commit()
    with open_client(engine_web) as client:
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
    with open_client(engine_web) as client:
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
    security.sessions[client.cookies.get("portfolio_session")].activity = -1800
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


def test_secure_cookie_option(engine_web, monkeypatch):
    with TestClient(
        create_app(engine_web, PASSWORD, secure_cookie=True), base_url="https://localhost"
    ) as secure_client:
        monkeypatch.setattr(secure_client.app.state.security, "_now", lambda: 10000)
        response = secure_client.get("/login")
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-portfolio_session=")
        for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/", "Max-Age=600"):
            assert attribute in cookie
        assert "Domain=" not in cookie
        original = secure_client.cookies.get("__Host-portfolio_session")
        response = secure_client.post(
            "/login",
            data={"csrf": token(response), "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert secure_client.cookies.get("__Host-portfolio_session") != original
        assert "Max-Age=1800" in response.headers["set-cookie"]
        csrf = token(secure_client.get("/p/1"))
        assert secure_client.get("/logout").status_code == 405
        assert secure_client.post("/logout", data={"csrf": "invalid"}).status_code == 403
        response = secure_client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["set-cookie"].startswith('__Host-portfolio_session="";')
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert not secure_client.cookies.get("__Host-portfolio_session")


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
        with open_client(empty) as empty_client:
            sign_in(empty_client)
            response = empty_client.get("/")
            assert response.status_code == 200
            assert "No portfolios yet" in response.text
    finally:
        empty.dispose()


def test_stored_text_is_escaped(client, engine_web):
    sign_in(client)
    with Session(engine_web) as session:
        session.get(Instrument, 1).name = '<script>alert("private")</script>'
        session.commit()
    response = client.get("/p/1")
    assert '<script>alert("private")</script>' not in response.text
    assert "&lt;script&gt;" in response.text


def test_session_capacity_returns_service_unavailable(client):
    security = client.app.state.security
    now = security._now()
    security.sessions = {str(n): BrowserSession("fake", now, now, True) for n in range(1024)}
    csrf = token(client.get("/login"))
    response = client.post(
        "/login", data={"csrf": csrf, "password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 503
    assert response.headers["x-frame-options"] == "DENY"
    assert sum(session.authenticated for session in security.sessions.values()) == 1024


@pytest.mark.parametrize(
    "path,status",
    [("/", 303), ("/p/1/imports", 303), ("/p/1/reconcile", 303), ("/missing", 303)],
)
def test_anonymous_requests_do_not_allocate_sessions(client, path, status):
    # A stream of clients without cookies must leave room for a new sign-in form.
    for _ in range(10):
        client.cookies.clear()
        response = client.get(path, follow_redirects=False)
        assert response.status_code == status
        assert "set-cookie" not in response.headers
    assert not client.app.state.security.sessions
    response = client.get("/login")
    assert token(response)
    assert len(client.app.state.security.sessions) == 1
    assert client.get("/login").status_code == 200
    assert len(client.app.state.security.sessions) == 1


def test_full_anonymous_pool_allows_sign_in_without_evicting_authenticated(client):
    security = client.app.state.security
    sign_in(client)
    authenticated_id = client.cookies.get("portfolio_session")
    for _ in range(1100):
        client.cookies.clear()
        assert client.get("/login").status_code == 200
    assert len(security.sessions) == 1025
    assert authenticated_id in security.sessions
    sign_in(client)
    assert sum(session.authenticated for session in security.sessions.values()) == 2


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
    with TestClient(client.app, base_url=BASE_URL, client=("100.100.20.30", 12345)) as other:
        sign_in(other)


def test_login_global_ceiling_and_window_expiry(client, monkeypatch):
    from portfolio_digest.web import security as security_module

    now = 10000
    monkeypatch.setattr(security_module.time, "monotonic", lambda: now)
    # Oversized passwords still count, without paying the scrypt cost for this test.
    for address in range(10):
        with TestClient(
            client.app, base_url=BASE_URL, client=(f"100.100.20.{address}", 12345)
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


PROTECTED_PATHS = [
    "/",
    "/p/1",
    "/p/1/holding/1",
    "/p/1/imports",
    "/p/1/reconcile",
    "/p/1/trade",
    "/missing",
    "/logout",
]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_signed_out_pages_redirect_before_loading_portfolios(client, engine_web, method, path):
    from sqlalchemy import event

    def no_query(*args):
        pytest.fail("Signed-out requests must not query portfolio data")

    event.listen(engine_web, "before_cursor_execute", no_query)
    try:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")
        assert not client.app.state.security.sessions
        login = client.get(response.headers["location"])
        assert login.status_code == 200
        assert "Kōwhai" not in login.text
        assert "Fern Labs" not in login.text
    finally:
        event.remove(engine_web, "before_cursor_execute", no_query)


@pytest.mark.parametrize("path", ["/p/1/trade", "/p/1/reconcile", "/missing", "/logout"])
def test_signed_out_writes_require_login(client, path):
    assert client.post(path, data={"csrf": ""}).status_code == 401


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
@pytest.mark.parametrize("path", PROTECTED_PATHS + ["/login", "/static/dashboard.css"])
def test_untrusted_hosts_rejected_before_sessions(client, method, path):
    response = client.request(
        method, path, headers={"host": "evil.example:8000"}, follow_redirects=False
    )
    assert response.status_code == 400
    assert "location" not in response.headers
    assert not client.app.state.security.sessions


@pytest.mark.parametrize(
    "bind,extra,host",
    [
        ("127.0.0.1", "", "127.0.0.1"),
        ("127.0.0.1", "", "127.0.0.1:8000"),
        ("127.0.0.1", "", "localhost"),
        ("127.0.0.1", "", "localhost:8000"),
        ("100.100.20.30", "", "100.100.20.30:8000"),
        ("::1", "", "[::1]"),
        ("::1", "", "[::1]:8000"),
        ("fd7a:115c:a1e0::1234", "", "[fd7a:115c:a1e0::1234]:8000"),
        ("127.0.0.1", "machine.tailnet.ts.net", "machine.tailnet.ts.net"),
        ("127.0.0.1", "machine.tailnet.ts.net", "machine.tailnet.ts.net:443"),
    ],
)
def test_configured_hosts_are_accepted(engine_web, bind, extra, host):
    with open_client(engine_web, bind=bind, hostnames=extra) as browser:
        assert browser.get("/login", headers={"host": host}).status_code == 200
        response = browser.get("/login", headers={"host": "unconfigured.example"})
        assert response.status_code == 400
        assert "location" not in response.headers


@pytest.mark.parametrize(
    "target",
    [
        "/p/1/imports?sort=date&direction=desc",
        "/p/1/imports?filter=a%26sort%3Db",
        "/p/1/imports?filter=a%23b",
        "/p/1/imports?filter=a%2526b",
        "https://other.example/",
        "//other.example/",
        "/\\other.example/",
        "/%2fother.example/",
        "/%252fother.example/",
        "/%5cother.example/",
        "/%0d%0aother.example/",
    ],
)
def test_login_validates_and_preserves_return_path(client, target):
    expected = target if target.startswith("/p/") else "/"
    response = client.get("/login", params={"next": target})
    from html import unescape

    carried = unescape(re.search(r'name="next" value="([^"]*)"', response.text).group(1))
    assert carried == expected
    csrf = token(response)
    failure = client.post("/login", data={"csrf": csrf, "password": "wrong", "next": target})
    assert failure.status_code == 401
    assert unescape(re.search(r'name="next" value="([^"]*)"', failure.text).group(1)) == expected
    response = client.post(
        "/login",
        data={"csrf": csrf, "password": PASSWORD, "next": target},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == expected


@pytest.mark.parametrize(
    "target",
    ["/p/1/imports?filter=a%26b", "/p/%31/imports?filter=a%23b"],
)
def test_signed_out_redirect_preserves_encoded_request_target(client, target):
    response = client.get(target, follow_redirects=False)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query)["next"] == [target]


@pytest.fixture
def session_clock(client, monkeypatch):
    clock = [10000]
    monkeypatch.setattr(client.app.state.security, "_now", lambda: clock[0])
    return clock


def test_anonymous_session_expires_at_ten_minutes(client, session_clock):
    response = client.get("/login")
    first = client.cookies.get("portfolio_session")
    assert "Max-Age=600" in response.headers["set-cookie"]
    session_clock[0] += 599
    assert client.get("/login").status_code == 200
    assert client.cookies.get("portfolio_session") == first
    session_clock[0] += 1
    assert client.get("/login").status_code == 200
    assert client.cookies.get("portfolio_session") != first
    assert first not in client.app.state.security.sessions


def test_authenticated_idle_expiry_and_static_does_not_refresh(client, session_clock):
    sign_in(client)
    ident = client.cookies.get("portfolio_session")
    session_clock[0] += 1799
    assert client.get("/p/1").status_code == 200
    session_clock[0] += 1799
    assert client.get("/static/dashboard.css").status_code == 200
    session_clock[0] += 1
    response = client.get("/p/1", follow_redirects=False)
    assert response.status_code == 303
    assert ident not in client.app.state.security.sessions


def test_activity_does_not_extend_absolute_expiry(client, session_clock):
    sign_in(client)
    ident = client.cookies.get("portfolio_session")
    for _ in range(28):
        session_clock[0] += 1000
        assert client.get("/p/1").status_code == 200
    session_clock[0] += 799
    response = client.get("/p/1")
    assert response.status_code == 200
    assert "Max-Age=1;" in response.headers["set-cookie"]
    session_clock[0] += 1
    assert client.get("/p/1", follow_redirects=False).status_code == 303
    assert ident not in client.app.state.security.sessions


def test_anonymous_pool_evicts_oldest_and_reclaims_expired(client, session_clock):
    security = client.app.state.security
    for n in range(1024):
        security.sessions[str(n)] = BrowserSession("fake", session_clock[0], session_clock[0])
    client.get("/login")
    assert "0" not in security.sessions
    assert "1" in security.sessions
    assert len(security.sessions) == 1024
    session_clock[0] += 600
    client.cookies.clear()
    client.get("/login")
    assert len(security.sessions) == 1


def test_expired_authenticated_pool_allows_new_login(client, session_clock):
    security = client.app.state.security
    security.sessions = {
        str(n): BrowserSession("fake", session_clock[0], session_clock[0], True)
        for n in range(1024)
    }
    session_clock[0] += 1800
    sign_in(client)
    assert len(security.sessions) == 1
