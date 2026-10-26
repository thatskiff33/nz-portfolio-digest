"""Server-rendered private dashboard. No portfolio data leaves this server."""

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from portfolio_digest.db import make_engine
from portfolio_digest.importers.base import FileProblem, RowProblem
from portfolio_digest.importing import ImportFailure, undo_import
from portfolio_digest.manual import add_trade, prepare_trade
from portfolio_digest.models import ImportRun, Income, Instrument, Portfolio, Price, Trade
from portfolio_digest.reconcile import compare, parse_snapshot, stored_units
from portfolio_digest.returns_loader import first_activity_date
from portfolio_digest.web.security import COOKIE, LIFETIME, Security
from portfolio_digest.web.viewmodel import Dashboard, period_dates

ROOT = Path(__file__).parent


def create_app(
    engine=None,
    password: str | None = None,
    *,
    timezone="Pacific/Auckland",
    secure_cookie: bool = False,
) -> FastAPI:
    engine = engine if engine is not None else make_engine()
    security = Security(
        password if password is not None else os.environ.get("DASHBOARD_PASSWORD", "")
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.security = security
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.filters["money"] = lambda n: "Unavailable" if n is None else f"{n:,.2f}"
    templates.env.filters["quantity"] = lambda n: format(n.normalize(), "f")
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        try:
            if not request.url.path.startswith("/static/"):
                request.state.session_id, request.state.browser = security.browser(request)
        except HTTPException as exc:
            response = HTMLResponse(str(exc.detail), status_code=exc.status_code)
        else:
            response = await call_next(request)
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
        if hasattr(request.state, "session_id"):
            response.set_cookie(
                COOKIE,
                request.state.session_id,
                max_age=LIFETIME,
                httponly=True,
                samesite="strict",
                secure=secure_cookie,
            )
        return response

    @app.exception_handler(HTTPException)
    async def error(request, exc):
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": exc.detail},
            status_code=exc.status_code,
        )

    def render(request, session, name, **context):
        context.update(portfolios=list(session.scalars(select(Portfolio).order_by(Portfolio.name))))
        return templates.TemplateResponse(request=request, name=name, context=context)

    def portfolio(session, ident):
        item = session.get(Portfolio, ident)
        if item is None:
            raise HTTPException(404, "Portfolio not found.")
        return item

    async def form(request, *, login=True):
        if login:
            security.require_login(request)
        size = 0
        parts = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                raise HTTPException(413, "Form too large.")
            parts.append(chunk)
        request._body = b"".join(parts)

        data = await request.form(max_fields=30, max_files=0)
        security.csrf(request, str(data.get("csrf", "")))
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
        with Session(engine) as session:
            return render(request, session, "login.html")

    @app.post("/login")
    async def login(request: Request):
        data = await form(request, login=False)
        security.login(request, str(data.get("password", "")))
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        await form(request)
        security.sessions.pop(request.state.session_id, None)
        request.state.session_id, request.state.browser = security.browser(request)
        return RedirectResponse("/", status_code=303)

    @app.get("/p/{pid}", response_class=HTMLResponse)
    def overview(
        request: Request,
        pid: int,
        period: str = "month",
        start: str = "",
        end: str = "",
        sort: str = "code",
        direction: str = "asc",
    ):
        with Session(engine) as session:
            p = portfolio(session, pid)
            today = datetime.now(ZoneInfo(timezone)).date()
            first = first_activity_date(session, pid, today) or today
            try:
                begin, finish = period_dates(period, today, first, start, end)
            except ValueError:
                raise HTTPException(
                    400, "Choose a valid period and dates ending no later than today."
                ) from None
            values = Dashboard(session, p, begin, finish)
            if sort not in {"code", "name", "quantity", "price", "value"} or direction not in {
                "asc",
                "desc",
            }:
                raise HTTPException(400, "Choose a valid sort order.")

            def key(row):
                value = (
                    getattr(row.instrument, sort)
                    if sort in {"code", "name"}
                    else getattr(row, sort)
                )
                return value is None, value if value is not None else 0

            rows = sorted(values.holdings, key=key, reverse=direction == "desc")
            markets = {}
            for row in rows:
                markets.setdefault(row.instrument.exchange, []).append(row)
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
                markets=dict(sorted(markets.items())),
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
            prices = list(
                session.scalars(
                    select(Price).where(Price.instrument_id == iid).order_by(Price.price_date)
                )
            )
            today = datetime.now(ZoneInfo(timezone)).date()
            begin, finish = period_dates(
                "all", today, first_activity_date(session, pid, today) or today
            )
            values = Dashboard(session, p, begin, finish)
            result = next((row.result for row in values.holdings if row.instrument.id == iid), None)
            return render(
                request,
                session,
                "holding.html",
                result=result,
                returns_error=values.error,
                start=begin,
                end=finish,
                portfolio=p,
                instrument=instrument,
                trades=trades,
                income=income,
                series=[(r.price_date, r.close) for r in prices],
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
        return RedirectResponse(f"/p/{pid}/imports", status_code=303)

    @app.get("/p/{pid}/trade", response_class=HTMLResponse)
    def trade_page(request: Request, pid: int):
        security.require_login(request)
        with Session(engine) as session:
            return render(request, session, "trade.html", portfolio=portfolio(session, pid))

    @app.post("/p/{pid}/trade")
    async def trade(request: Request, pid: int):
        data = await form(request)
        with Session(engine) as session:
            p = portfolio(session, pid)
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
            except (RowProblem, ValueError, ImportFailure):
                session.rollback()
                raise HTTPException(
                    400, "Trade not saved. Check the date, listing, type and amounts."
                ) from None
        return RedirectResponse(f"/p/{pid}", status_code=303)

    @app.get("/p/{pid}/reconcile", response_class=HTMLResponse)
    def reconcile_page(request: Request, pid: int):
        with Session(engine) as session:
            return render(request, session, "reconcile.html", portfolio=portfolio(session, pid))

    @app.post("/p/{pid}/reconcile", response_class=HTMLResponse)
    async def reconcile(request: Request, pid: int):
        data = await form(request)
        with Session(engine) as session:
            p = portfolio(session, pid)
            try:
                snapshot = parse_snapshot(str(data.get("snapshot", "")).encode())
                differences = compare(stored_units(session, pid), snapshot)
            except FileProblem:
                raise HTTPException(
                    400, "Snapshot not read. Use CSV with Exchange, Code and Units columns."
                ) from None
            return render(request, session, "reconcile.html", portfolio=p, differences=differences)

    return app


def main():
    """Compose entry point, with one worker for shared sessions and throttling."""
    import uvicorn

    from portfolio_digest.config import get_settings
    from portfolio_digest.web.binding import private_host

    private_host(os.environ.get("DASHBOARD_BIND", "127.0.0.1"))
    app = create_app(
        timezone=get_settings().timezone,
        secure_cookie=os.environ.get("DASHBOARD_SECURE_COOKIE", "false").lower() == "true",
    )
    uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=False, access_log=False)  # noqa: S104


if __name__ == "__main__":
    main()
