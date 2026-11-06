"""Server-rendered private dashboard. No portfolio data leaves this server."""

from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from portfolio_digest.config import get_settings
from portfolio_digest.db import make_engine
from portfolio_digest.importers.base import FileProblem, RowProblem
from portfolio_digest.importing import ImportFailure, undo_import
from portfolio_digest.manual import add_trade, prepare_trade
from portfolio_digest.models import ImportRun, Income, Instrument, Portfolio, Price, Trade
from portfolio_digest.reconcile import compare, parse_snapshot, stored_units
from portfolio_digest.returns_loader import first_activity_date
from portfolio_digest.web.security import Security
from portfolio_digest.web.viewmodel import Dashboard, period_dates

ROOT = Path(__file__).parent
_RETURN_PATH_LIMIT = 2048


def safe_return_path(value: object) -> str:
    """Same-site path beginning with one slash, or `/` when the value is not.

    Absolute URLs, protocol-relative URLs, backslashes, controls and encodings of
    those forms are rejected. The check is repeated after decoding so a second
    encoding cannot change the navigation origin.
    """
    if not isinstance(value, str):
        return "/"
    candidate = value
    for _ in range(5):
        if not _same_site(candidate):
            return "/"
        decoded = unquote(candidate)
        if decoded == candidate:
            return candidate
        candidate = decoded
    return "/"


def _same_site(value: str) -> bool:
    if not value or len(value) > _RETURN_PATH_LIMIT or not value.isascii():
        return False
    if not value.startswith("/") or value.startswith("//"):
        return False
    if any(char == "\\" or char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return "://" not in value.lower()


def _current_target(request: Request) -> str:
    """Path and query as the app received them. Host and forwarded headers are ignored."""
    path = request.scope.get("path") or "/"
    raw_query = request.scope.get("query_string", b"")
    query = raw_query.decode("latin-1", "ignore") if isinstance(raw_query, bytes) else str(raw_query)
    if query:
        return f"{path}?{query}"
    return path


def see_other(location: str) -> Response:
    return Response(status_code=303, headers={"location": location})


def _login_location(request: Request) -> str:
    target = safe_return_path(_current_target(request))
    return "/login?next=" + quote(target, safe="/")


def allowed_hosts(bind: str, hostnames: str) -> list[str]:
    """Bind address, loopback names, and configured hostnames. Ports are not part of the name.

    Starlette compares the host with IPv6 brackets preserved and the port removed, so an
    IPv6 bind is also listed in brackets. The list is never widened to every IPv6 address.
    """
    hosts: list[str] = []

    def add(value: str) -> None:
        item = value.strip().lower()
        if not item or any(char in item for char in "*/\\"):
            return
        if item not in hosts:
            hosts.append(item)
        literal = item[1:-1] if item.startswith("[") and item.endswith("]") else item
        try:
            address = ip_address(literal)
        except ValueError:
            return
        if address.version == 6:
            bracketed = f"[{literal}]"
            if bracketed not in hosts:
                hosts.append(bracketed)

    add("127.0.0.1")
    add("localhost")
    add(bind)
    for name in hostnames.split(","):
        add(name)
    return hosts


def create_app(
    engine=None,
    password: str | None = None,
    *,
    timezone: str = "Pacific/Auckland",
    secure_cookie: bool = False,
    bind: str = "127.0.0.1",
    hostnames: str = "",
) -> FastAPI:
    if password is None:
        settings = get_settings()
        secret = settings.dashboard_password
        password = "" if secret is None else secret.get_secret_value()
        timezone = settings.timezone
        secure_cookie = settings.dashboard_secure_cookie
        bind = settings.dashboard_bind
        hostnames = settings.dashboard_hostnames
    engine = engine if engine is not None else make_engine()
    security = Security(password, secure_cookie=secure_cookie)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.security = security
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.filters["money"] = lambda n: "Unavailable" if n is None else f"{n:,.2f}"
    templates.env.filters["quantity"] = lambda n: format(n.normalize(), "f")
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    def public(request: Request) -> bool:
        path = request.url.path
        method = request.method
        if path == "/static" or path.startswith("/static/"):
            return True
        if path == "/login" and method in {"GET", "HEAD", "POST"}:
            return True
        return path == "/logout" and method == "POST"

    @app.middleware("http")
    async def headers(request: Request, call_next):
        path = request.url.path
        static = path == "/static" or path.startswith("/static/")
        # Static files must not create a session or refresh the idle timer.
        denied = None
        if not static:
            try:
                request.state.session_id, request.state.browser = security.browser(request)
            except HTTPException as exc:
                denied = HTMLResponse(str(exc.detail), status_code=exc.status_code)
        if denied is not None:
            response = denied
        elif static or public(request) or request.state.browser.authenticated:
            response = await call_next(request)
        elif request.method in {"GET", "HEAD"}:
            response = see_other(_login_location(request))
        else:
            response = HTMLResponse("Sign in to continue.", status_code=401)
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
        session_id = getattr(request.state, "session_id", "")
        browser = getattr(request.state, "browser", None)
        if session_id and browser is not None:
            response.set_cookie(
                security.cookie_name,
                session_id,
                max_age=security.cookie_max_age(browser),
                httponly=True,
                samesite="strict",
                secure=secure_cookie,
                path="/",
            )
        return response

    # Added last so it runs before session handling. A rejected host does not redirect
    # and does not reach the code that allocates a session. www_redirect stays off.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts(bind, hostnames),
        www_redirect=False,
    )

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

    def login_form(request: Request, nxt: str, status_code: int = 200, message: str = ""):
        # No portfolio query: the sign-in page has no sidebar data.
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"next": nxt, "message": message},
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with Session(engine) as session:
            first = session.scalar(select(Portfolio).order_by(Portfolio.name))
            if first:
                return RedirectResponse(f"/p/{first.id}", status_code=303)
            return render(request, session, "empty.html")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = ""):
        request.state.session_id, request.state.browser = security.browser(request, create=True)
        return login_form(request, safe_return_path(next))

    @app.post("/login")
    async def login(request: Request):
        data = await form(request, login=False)
        nxt = safe_return_path(str(data.get("next", "")))
        try:
            security.login(request, str(data.get("password", "")))
        except HTTPException as exc:
            if exc.status_code == 401:
                return login_form(request, nxt, status_code=401, message=str(exc.detail))
            raise
        return see_other(nxt)

    @app.post("/logout")
    async def logout(request: Request):
        await form(request)
        with security.lock:
            security.sessions.pop(request.state.session_id, None)
        request.state.session_id, request.state.browser = security.browser(request)
        response = see_other("/")
        response.delete_cookie(
            security.cookie_name,
            path="/",
            httponly=True,
            samesite="strict",
            secure=secure_cookie,
        )
        return response

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

    from portfolio_digest.web.binding import private_host

    settings = get_settings()
    private_host(settings.dashboard_bind)
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=False, access_log=False)  # noqa: S104


if __name__ == "__main__":
    main()
