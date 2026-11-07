"""Server-rendered private dashboard. No portfolio data leaves this server."""

from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from portfolio_digest.config import Settings, get_settings
from portfolio_digest.db import make_engine
from portfolio_digest.formatting import display_number, display_quantity
from portfolio_digest.importers.base import FileProblem, RowProblem
from portfolio_digest.importing import ImportFailure, undo_import
from portfolio_digest.manual import TradeProblem, add_trade, prepare_trade
from portfolio_digest.models import ImportRun, Income, Instrument, Portfolio, Trade
from portfolio_digest.reconcile import compare, parse_snapshot, stored_units
from portfolio_digest.returns import ReturnError, holding_return
from portfolio_digest.returns_loader import first_activity_date, load_portfolio_inputs
from portfolio_digest.web.hosts import DashboardTrustedHostMiddleware
from portfolio_digest.web.security import ANONYMOUS_LIFETIME, LIFETIME, Security
from portfolio_digest.web.viewmodel import Dashboard, effective_period, period_dates

ROOT = Path(__file__).parent
# Dashboard forms are small; refuse anything larger before parsing it.
FORM_MAX_BYTES = 64 * 1024
FORM_MAX_FIELDS = 30


def safe_next(value: str) -> str:
    """Accept only local paths, including after URL decoding by a browser."""
    decoded = value
    for _ in range(5):
        if (
            not decoded.startswith("/")
            or decoded.startswith("//")
            or chr(92) in decoded
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        ):
            return "/"
        expanded = unquote(decoded)
        if expanded == decoded:
            return value
        decoded = expanded
    return "/"


def group_holdings(holdings, sort, direction):
    """Validate the requested order, sort holdings and group them by exchange."""
    if sort not in {"code", "name", "quantity", "price", "value"} or direction not in {
        "asc",
        "desc",
    }:
        raise HTTPException(400, "Choose a valid sort order.")

    def key(row):
        value = getattr(row.instrument, sort) if sort in {"code", "name"} else getattr(row, sort)
        return value is None, value if value is not None else 0

    rows = sorted(holdings, key=key, reverse=direction == "desc")
    markets = {}
    for row in rows:
        markets.setdefault(row.instrument.exchange, []).append(row)
    return dict(sorted(markets.items()))


def create_app(
    engine=None,
    password: str | None = None,
    *,
    timezone="Pacific/Auckland",
    secure_cookie: bool | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or (
        get_settings() if engine is None else Settings(database_url=str(engine.url))
    )
    engine = engine if engine is not None else make_engine()
    secure_cookie = settings.dashboard_secure_cookie if secure_cookie is None else secure_cookie
    security = Security(
        password if password is not None else settings.dashboard_password.get_secret_value(),
        secure_cookie=secure_cookie,
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.security = security
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.filters["money"] = lambda n: "Unavailable" if n is None else display_number(n)
    templates.env.filters["quantity"] = display_quantity
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/healthz")
    def healthz() -> PlainTextResponse:
        """Liveness only. Does not open a session or read the database."""
        return PlainTextResponse("ok")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        try:
            request.state.session_id, request.state.browser = security.browser(request)
            public = (
                request.url.path == "/login"
                or (request.url.path == "/logout" and request.method == "POST")
                or request.url.path == "/static"
                or request.url.path.startswith("/static/")
            )
            if not public and not request.state.browser.authenticated:
                if request.method in {"GET", "HEAD"}:
                    target = request.url.path
                    if request.url.query:
                        target += "?" + request.url.query
                    response = RedirectResponse(
                        "/login?" + urlencode({"next": target}), status_code=303
                    )
                else:
                    response = HTMLResponse("Sign in to view your portfolio.", status_code=401)
            else:
                response = await call_next(request)
        except HTTPException as exc:
            response = HTMLResponse(str(exc.detail), status_code=exc.status_code)
        response.headers.update(
            {
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        if getattr(request.state, "session_id", ""):
            response.set_cookie(
                security.cookie,
                request.state.session_id,
                max_age=LIFETIME if request.state.browser.authenticated else ANONYMOUS_LIFETIME,
                httponly=True,
                samesite="strict",
                secure=secure_cookie,
            )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def error(request, exc):
        if not hasattr(request.state, "browser"):
            request.state.session_id, request.state.browser = security.browser(request)
        with Session(engine) as session:
            return render(
                request,
                session,
                "error.html",
                status_code=exc.status_code,
                message=exc.detail,
                error_status=exc.status_code,
            )

    def render(request, session, name, *, status_code=200, **context):
        context.update(
            portfolios=list(session.scalars(select(Portfolio).order_by(Portfolio.name)))
            if request.state.browser.authenticated
            else [],
            page=name.removesuffix(".html"),
        )
        context.setdefault("values", {})
        context.setdefault("errors", {})
        context.setdefault("form_error", "")
        context.setdefault("status_message", security.take_message(request))
        return templates.TemplateResponse(
            request=request,
            name=name,
            context=context,
            status_code=status_code,
        )

    def expired_form(request, session, p, name, data):
        return render(
            request,
            session,
            name,
            status_code=403,
            portfolio=p,
            values=data,
            form_error="This form has expired. Check your entries and resubmit.",
        )

    def portfolio(session, ident):
        item = session.get(Portfolio, ident)
        if item is None:
            raise HTTPException(404, "Portfolio not found.")
        return item

    async def form(request, *, login=True, recover_csrf=False):
        if login:
            security.require_login(request)
        size = 0
        parts = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > FORM_MAX_BYTES:
                raise HTTPException(413, "Form too large.")
            parts.append(chunk)
        request._body = b"".join(parts)

        data = await request.form(max_fields=FORM_MAX_FIELDS, max_files=0)
        request.state.csrf_expired = False
        try:
            security.csrf(request, str(data.get("csrf", "")))
        except HTTPException:
            if not recover_csrf:
                raise
            request.state.csrf_expired = True
        return data

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with Session(engine) as session:
            first = session.scalar(select(Portfolio).order_by(Portfolio.name))
            if first:
                return RedirectResponse(f"/p/{first.id}", status_code=303)
            return render(request, session, "empty.html")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        request.state.session_id, request.state.browser = security.browser(request, create=True)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next_path": safe_next(request.query_params.get("next", "/"))},
        )

    @app.post("/login")
    async def login(request: Request):
        data = await form(request, login=False)
        security.login(request, str(data.get("password", "")))
        return RedirectResponse(safe_next(str(data.get("next", "/"))), status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        await form(request)
        security.sessions.pop(request.state.session_id, None)
        request.state.session_id, request.state.browser = security.browser(request)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(
            security.cookie, httponly=True, samesite="strict", secure=secure_cookie
        )
        return response

    @app.get("/p/{pid}", response_class=HTMLResponse)
    def overview(
        request: Request,
        pid: int,
        period: str = "month",
        start: str = "",
        end: str = "",
        original_start: str = "",
        original_end: str = "",
        sort: str = "code",
        direction: str = "asc",
    ):
        with Session(engine) as session:
            p = portfolio(session, pid)
            as_of = datetime.now(ZoneInfo(timezone))
            today = as_of.date()
            first = first_activity_date(session, pid, today) or today
            try:
                begin, finish = period_dates(
                    period, today, first, start, end, original_start, original_end
                )
                period = effective_period(
                    period, today, first, start, end, original_start, original_end
                )
            except ValueError:
                raise HTTPException(
                    400, "Choose a valid period and dates ending no later than today."
                ) from None
            values = Dashboard(session, p, begin, finish, as_of=as_of)
            markets = group_holdings(values.holdings, sort, direction)
            subtotals = {r.label: r for r in values.report.markets} if values.report else {}
            return render(
                request,
                session,
                "overview.html",
                portfolio=p,
                period=period,
                start=begin,
                end=finish,
                dashboard=values,
                report=values.report,
                markets=markets,
                subtotals=subtotals,
                series=values.series(begin, finish),
                sort=sort,
                direction=direction,
            )

    @app.get("/p/{pid}/holding/{iid}", response_class=HTMLResponse)
    def holding(request: Request, pid: int, iid: int):
        with Session(engine) as session:
            p = portfolio(session, pid)
            trades = list(
                session.scalars(
                    select(Trade)
                    .where(Trade.portfolio_id == pid, Trade.instrument_id == iid)
                    .order_by(Trade.trade_date.desc(), Trade.id.desc())
                )
            )
            income = list(
                session.scalars(
                    select(Income)
                    .where(Income.portfolio_id == pid, Income.instrument_id == iid)
                    .order_by(Income.paid_date.desc())
                )
            )
            if not trades and not income:
                raise HTTPException(404, "Holding not found in this portfolio.")
            instrument = session.get(Instrument, iid)
            as_of = datetime.now(ZoneInfo(timezone))
            today = as_of.date()
            activity = [t.trade_date for t in trades if t.trade_date <= today] + [
                i.paid_date
                for i in income
                if i.paid_date <= today and i.kind in ("dividend", "distribution")
            ]
            begin, finish = period_dates("all", today, min(activity, default=today))
            result, error = None, None
            prices = ()
            try:
                data = load_portfolio_inputs(
                    session, p, begin, finish, instrument_id=iid, as_of=as_of
                )
                prices = data.holdings[0].prices if data.holdings else ()
                if data.holdings:
                    result = holding_return(
                        data.holdings[0], begin, finish, data.rates, data.base_currency, as_of=as_of
                    )
            except ReturnError as exc:
                error = str(exc)
            count = len(prices)
            indices = sorted({(count - 1) * n // 179 for n in range(180)}) if count else []
            return render(
                request,
                session,
                "holding.html",
                result=result,
                returns_error=error,
                start=begin,
                end=finish,
                portfolio=p,
                instrument=instrument,
                trades=trades,
                income=income,
                series=[(prices[n].day, prices[n].value) for n in indices],
            )

    @app.get("/p/{pid}/imports", response_class=HTMLResponse)
    def imports(request: Request, pid: int):
        with Session(engine) as session:
            p = portfolio(session, pid)
            runs = list(
                session.scalars(
                    select(ImportRun)
                    .where(ImportRun.portfolio_id == pid)
                    .order_by(ImportRun.id.desc())
                )
            )
            return render(request, session, "imports.html", portfolio=p, runs=runs)

    @app.post("/p/{pid}/imports/{rid}/undo")
    async def undo(request: Request, pid: int, rid: int):
        await form(request)
        with Session(engine) as session:
            portfolio(session, pid)
            run = session.get(ImportRun, rid)
            if not run or run.portfolio_id != pid:
                raise HTTPException(404, "Import not found in this portfolio.")
            try:
                undo_import(session, rid)
            except ImportFailure as exc:
                raise HTTPException(400, str(exc)) from None
        security.flash(request, "Import undone.")
        return RedirectResponse(f"/p/{pid}/imports", status_code=303)

    @app.get("/p/{pid}/trade", response_class=HTMLResponse)
    def trade_page(request: Request, pid: int):
        security.require_login(request)
        with Session(engine) as session:
            return render(request, session, "trade.html", portfolio=portfolio(session, pid))

    @app.post("/p/{pid}/trade")
    async def trade(request: Request, pid: int):
        data = await form(request, recover_csrf=True)
        with Session(engine) as session:
            p = portfolio(session, pid)
            if request.state.csrf_expired:
                return expired_form(request, session, p, "trade.html", data)
            try:
                fields = {
                    k: str(data.get(k, ""))
                    for k in (
                        "exchange",
                        "code",
                        "trade_date",
                        "trade_type",
                        "quantity",
                        "price",
                        "fees",
                        "notes",
                    )
                }
                add_trade(session, p.name, prepare_trade(**fields))
                session.commit()
            except (RowProblem, ValueError, ImportFailure) as exc:
                session.rollback()
                errors = (
                    {field: str(exc) for field in exc.fields}
                    if isinstance(exc, TradeProblem)
                    else {}
                )
                return render(
                    request,
                    session,
                    "trade.html",
                    status_code=400,
                    portfolio=p,
                    values=fields,
                    errors=errors,
                    form_error="Trade not saved. Check your entries and try again.",
                )
        security.flash(request, "Trade saved.")
        return RedirectResponse(f"/p/{pid}", status_code=303)

    @app.get("/p/{pid}/reconcile", response_class=HTMLResponse)
    def reconcile_page(request: Request, pid: int):
        with Session(engine) as session:
            return render(request, session, "reconcile.html", portfolio=portfolio(session, pid))

    @app.post("/p/{pid}/reconcile", response_class=HTMLResponse)
    async def reconcile(request: Request, pid: int):
        data = await form(request, recover_csrf=True)
        with Session(engine) as session:
            p = portfolio(session, pid)
            if request.state.csrf_expired:
                return expired_form(request, session, p, "reconcile.html", data)
            try:
                snapshot = parse_snapshot(str(data.get("snapshot", "")).encode())
                differences = compare(stored_units(session, pid), snapshot)
            except (FileProblem, RowProblem, ValueError):
                return render(
                    request,
                    session,
                    "reconcile.html",
                    status_code=400,
                    portfolio=p,
                    values=data,
                    errors={
                        "snapshot": "Use CSV with Exchange, Code and Units columns. "
                        "Check each listing and units value."
                    },
                    form_error="Snapshot not read. Check your entries and try again.",
                )
            message = (
                "All holdings match the snapshot."
                if not differences
                else f"Comparison complete. {len(differences)} holdings differ."
            )
            return render(
                request,
                session,
                "reconcile.html",
                portfolio=p,
                differences=differences,
                values=data,
                status_message=message,
            )

    app.add_middleware(
        DashboardTrustedHostMiddleware,
        allowed_hosts=[settings.dashboard_bind, "127.0.0.1", "localhost"]
        + [host.strip() for host in settings.dashboard_hostnames.split(",") if host.strip()],
        www_redirect=False,
    )
    return app


def main():
    """Compose entry point, with one worker for shared sessions and throttling."""
    import uvicorn

    from portfolio_digest.web.binding import private_host

    settings = get_settings()
    private_host(settings.dashboard_bind)
    app = create_app(timezone=settings.timezone, settings=settings)
    uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=False, access_log=False)  # noqa: S104


if __name__ == "__main__":
    main()
