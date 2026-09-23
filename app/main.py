"""FastAPI-Anwendung: HTML-Views, JSON-API, Scheduler."""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app import (
    access,
    csrf,
    dashboard,
    momentum_view,
    symbols,
    ticker_view,
    views,
    watchlist,
    watchlist_view,
)
from app.config import Settings, load_settings
from app.db import get_sessionmaker, init_engine, session_scope
from app.logging_conf import configure_logging
from app.pipeline import (
    PipelineBusy,
    background_state,
    request_background_run,
    run_daily,
)
from app.scheduler import start_scheduler

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["csrf_token"] = csrf.token
templates.env.globals["csrf_field"] = csrf.FIELD

STATE: dict[str, object] = {}


def get_settings() -> Settings:
    settings = STATE.get("settings")
    if settings is None:  # pragma: no cover
        raise RuntimeError("Settings sind nicht initialisiert.")
    return settings  # type: ignore[return-value]


def get_session():
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
    finally:
        session.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    configure_logging(settings.log_level)
    log.info("PEAD-Signal-POC startet.")

    init_engine(settings.db_path)
    with session_scope() as session:
        seed = watchlist.seed_tickers(session, settings.tickers)
        beobachtet = len(watchlist.active_symbols(session))
    log.info("%d Titel in Beobachtung.", beobachtet)
    if seed.ignored:
        # Wer config.yaml bearbeitet, soll erfahren, dass die Datei nicht mehr wirkt.
        log.info(
            "config.yaml nennt %d Titel, die nicht beobachtet werden (%s). Die Liste "
            "wird in der Oberflaeche unter /titel gepflegt; config.yaml gilt nur "
            "fuer eine leere Datenbank.",
            len(seed.ignored), ", ".join(seed.ignored[:8]),
        )

    if access.enforcement_needed(settings.bind_addr):
        networks = access.parse_allowlist(settings.allowed_ips)
        STATE["allowed_networks"] = networks
        log.info(
            "Port ist auf %s veroeffentlicht - Zugriff erlaubt fuer: %s",
            settings.bind_addr,
            access.describe(networks),
        )
    else:
        # Loopback-Bindung: das Betriebssystem beschraenkt bereits.
        STATE["allowed_networks"] = None
        log.info(
            "Port nur auf %s veroeffentlicht - keine zusaetzliche IP-Pruefung.",
            settings.bind_addr,
        )

    STATE["settings"] = settings
    STATE["scheduler"] = start_scheduler(settings)

    if settings.run_on_startup:
        log.info("RUN_ON_STARTUP=true - fuehre einen Lauf beim Start aus.")
        try:
            run_daily(settings, trigger="startup")
        except Exception:  # pragma: no cover
            log.exception("Startlauf fehlgeschlagen - die App laeuft trotzdem weiter.")

    try:
        yield
    finally:
        scheduler = STATE.get("scheduler")
        if scheduler is not None:
            scheduler.shutdown(wait=False)  # type: ignore[union-attr]
        log.info("PEAD-Signal-POC beendet.")


app = FastAPI(title="PEAD-Signal-POC", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _rejected(request: Request, reason: str) -> object:
    return templates.TemplateResponse(
        request=request,
        name="rejected.html",
        context={"reason": reason},
        status_code=403,
    )


@app.middleware("http")
async def check_origin(request: Request, call_next):
    """Schreibende Anfragen nur von Seiten dieser Anwendung (siehe app/csrf.py).

    Vor restrict_by_ip registriert, damit die IP-Sperre aussen liegt und
    zuerst greift.
    """
    if csrf.origin_ok(request.method, request.headers):
        return await call_next(request)
    log.warning(
        "Schreibzugriff fremder Herkunft abgewiesen: %s %s (Origin %s, Referer %s)",
        request.method, request.url.path,
        request.headers.get("origin"), request.headers.get("referer"),
    )
    return _rejected(request, "herkunft")


async def _checked_form(request: Request) -> dict[str, list[str]] | None:
    """Formularfelder, falls das Token stimmt - sonst None."""
    try:
        form = csrf.parse_form(await request.body())
    except csrf.FormTooLarge:
        return None
    submitted = (form.get(csrf.FIELD) or [None])[0]
    return form if csrf.token_ok(submitted) else None


@app.middleware("http")
async def restrict_by_ip(request: Request, call_next):
    """Laesst nur Absender aus ALLOWED_IPS durch.

    Entschieden wird anhand der TCP-Gegenstelle. Weiterleitungskoepfe wie
    X-Forwarded-For werden bewusst ignoriert: ohne vorgeschalteten Proxy
    kann sie jeder selbst setzen.
    """
    networks = STATE.get("allowed_networks")
    if networks is None:
        # Entweder Loopback-Bindung oder die Lifespan lief noch nicht.
        return await call_next(request)

    client_ip = request.client.host if request.client else None
    if access.is_allowed(client_ip, networks):  # type: ignore[arg-type]
        return await call_next(request)

    log.warning("Zugriff abgewiesen: %s auf %s", client_ip, request.url.path)
    return PlainTextResponse(
        "Zugriff verweigert.\n\n"
        f"Deine Adresse: {client_ip or 'unbekannt'}\n"
        "Sie steht nicht in ALLOWED_IPS. Eintragen in der .env auf dem Server,\n"
        "danach 'make up' (nicht 'make restart').\n",
        status_code=403,
    )


# -- Jinja-Filter -----------------------------------------------------------


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "–"
    scaled = value * 100
    # Runden vor dem Formatieren, sonst wird aus -0.0001 ein "-0.00 %".
    if round(scaled, digits) == 0:
        scaled = 0.0
    return f"{scaled:+.{digits}f} %"


def _fmt_rate(value: float | None, digits: int = 0) -> str:
    """Anteil ohne Vorzeichen - eine Trefferquote ist keine Veraenderung."""
    return "–" if value is None else f"{value * 100:.{digits}f} %"


def _fmt_rate_bare(value: float | None, digits: int = 0) -> str:
    """Anteil ohne Einheit - fuer die untere Grenze eines Intervalls, damit
    das Prozentzeichen nur einmal dasteht und die Kachel nicht umbricht."""
    return "–" if value is None else f"{value * 100:.{digits}f}"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"{value:,.{digits}f}".replace(",", " ")


def _fmt_sue(value: float | None) -> str:
    return "–" if value is None else f"{value:+.2f}"


def _fmt_dt(value: dt.datetime | None) -> str:
    if value is None:
        return "–"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


templates.env.filters["pct"] = _fmt_pct
templates.env.filters["rate"] = _fmt_rate
templates.env.filters["ratebare"] = _fmt_rate_bare
templates.env.filters["num"] = _fmt_num
templates.env.filters["sue"] = _fmt_sue
def _fmt_date(value) -> str:
    if value is None:
        return "–"
    if isinstance(value, str):
        try:
            value = dt.date.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%d.%m.%Y")


templates.env.filters["dtfmt"] = _fmt_dt
templates.env.filters["datefmt"] = _fmt_date


# -- HTML -------------------------------------------------------------------


@app.get("/", response_class=None)
def index(request: Request, session: Session = Depends(get_session)):
    settings = get_settings()
    data = dashboard.build(session, settings)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "d": data,
            # Die Legende erwartet diese beiden Namen.
            "summary": data.pead_summary,
            "last_run": views.last_run(session),
            "settings": settings,
            "notice": request.query_params.get("notice"),
        },
    )


@app.get("/history", response_class=None)
def history(request: Request, session: Session = Depends(get_session)):
    geschlossene = views.closed_signals(session)
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "signals": geschlossene,
            "explanations": views.explanations_for(session, geschlossene, get_settings()),
            "summary": views.summary(session),
            "summary_retro": views.summary(session, retro=True),
            "last_run": views.last_run(session),
            "settings": get_settings(),
        },
    )


@app.get("/events", response_class=None)
def events(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request=request,
        name="events.html",
        context={
            "events": views.earnings_events(session),
            "summary": views.summary(session),
            "last_run": views.last_run(session),
            "settings": get_settings(),
        },
    )


@app.get("/titel", response_class=None)
def ticker_index(
    request: Request,
    q: str | None = Query(default=None, max_length=300),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    lookups = None
    if q and q.strip():
        lookups = symbols.resolve_many(q)
        watchlist_view.mark_known(session, lookups)
    tickers = ticker_view.ticker_overview(session, settings)
    background = background_state()
    return templates.TemplateResponse(
        request=request,
        name="tickers.html",
        context={
            "tickers": tickers,
            "q": (q or "").strip(),
            "lookups": lookups,
            "flash": watchlist_view.flash_from_query(request.query_params),
            "inactive": watchlist.inactive_tickers(session),
            "export_block": watchlist.config_block(session),
            "background": background,
            # Solange ein Lauf aussteht und Titel ohne Kurs dastehen, laedt die
            # Seite sich selbst neu - der Nutzer soll nicht raten muessen.
            "reload": background is not None and any(t["last_close"] is None for t in tickers),
            "last_run": views.last_run(session),
            "summary": views.summary(session),
            "settings": settings,
        },
    )


def _add_symbols(chosen: list[str], names: dict[str, str]) -> watchlist.Addition:
    with session_scope() as session:
        return watchlist.add(session, chosen, names)


def _remove_symbol(symbol: str) -> watchlist.Removal | None:
    with session_scope() as session:
        return watchlist.remove(session, symbol)


@app.post("/titel/aufnehmen")
async def ticker_add(request: Request):
    form = await _checked_form(request)
    if form is None:
        return _rejected(request, "token")

    chosen: list[str] = []
    names: dict[str, str] = {}
    for key, values in form.items():
        if key.startswith("wahl-"):
            symbol = symbols.normalize_symbol(values[0])
            if symbol and symbol not in chosen:
                chosen.append(symbol)
        elif key.startswith("n:"):
            symbol = symbols.normalize_symbol(key[2:])
            if symbol and values:
                names[symbol] = values[0][:128]
    if not chosen:
        return RedirectResponse(url="/titel?nichts=1", status_code=303)

    # In den Threadpool: laeuft gerade ein Lauf mit grosser Schreibtransaktion,
    # wartet SQLite - das soll nicht die ganze Anwendung anhalten.
    result = await run_in_threadpool(_add_symbols, chosen, names)
    if result.changed:
        request_background_run(get_settings())
    params = {
        key: ",".join(value)
        for key, value in (
            ("aufgenommen", result.added),
            ("reaktiviert", result.reactivated),
            ("bereits", result.already),
        )
        if value
    }
    return RedirectResponse(url=f"/titel?{urlencode(params)}", status_code=303)


@app.post("/titel/{symbol}/entfernen")
async def ticker_remove(
    request: Request, symbol: str = PathParam(pattern=r"^[A-Za-z0-9.\-]{1,16}$")
):
    if await _checked_form(request) is None:
        return _rejected(request, "token")
    removal = await run_in_threadpool(_remove_symbol, symbol.upper())
    if removal is None:
        raise HTTPException(status_code=404, detail=f"Unbekanntes Symbol: {symbol}")
    params = {"entfernt": removal.symbol}
    offen = removal.open_pead + removal.open_momentum
    if offen:
        params["offen"] = str(offen)
    return RedirectResponse(url=f"/titel?{urlencode(params)}", status_code=303)


@app.post("/titel/{symbol}/aufnehmen")
async def ticker_readd(
    request: Request, symbol: str = PathParam(pattern=r"^[A-Za-z0-9.\-]{1,16}$")
):
    form = await _checked_form(request)
    if form is None:
        return _rejected(request, "token")
    result = await run_in_threadpool(_add_symbols, [symbol.upper()], {})
    if result.changed:
        request_background_run(get_settings())
    if (form.get("zurueck") or [""])[0] == "steckbrief":
        return RedirectResponse(url=f"/titel/{symbol.upper()}", status_code=303)
    params = {"reaktiviert": ",".join(result.reactivated or result.added)}
    return RedirectResponse(url=f"/titel?{urlencode(params)}", status_code=303)


@app.get("/titel/{symbol}", response_class=None)
def ticker_page(
    request: Request,
    symbol: str = PathParam(pattern=r"^[A-Za-z0-9.\-]{1,16}$"),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    detail = ticker_view.ticker_detail(session, settings, symbol)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Unbekanntes Symbol: {symbol}")
    eigene_momentum = momentum_view.for_symbol(session, detail.symbol)
    return templates.TemplateResponse(
        request=request,
        name="ticker.html",
        context={
            "d": detail,
            "explanations": views.explanations_for(
                session, detail.open_signals + detail.closed_signals, settings
            ),
            "momentum": eigene_momentum,
            "momentum_explanations": momentum_view.explanations_for(
                session, eigene_momentum, settings
            ),
            "momentum_scores": momentum_view.current_scores(session, settings),
            "last_run": views.last_run(session),
            "summary": views.summary(session),
            "settings": settings,
        },
    )


@app.get("/momentum", response_class=None)
def momentum_page(request: Request, session: Session = Depends(get_session)):
    settings = get_settings()
    offene_momentum = momentum_view.open_positions(session)
    geschlossene_momentum = momentum_view.closed_positions(session)
    return templates.TemplateResponse(
        request=request,
        name="momentum.html",
        context={
            "open_positions": offene_momentum,
            "closed_positions": geschlossene_momentum,
            "explanations": {
                **momentum_view.explanations_for(session, offene_momentum, settings),
                **momentum_view.explanations_for(session, geschlossene_momentum, settings),
            },
            "scores": momentum_view.current_scores(session, settings),
            "readiness": momentum_view.readiness(session, settings),
            "summary": momentum_view.summary(session),
            "summary_retro": momentum_view.retro_summary(session, settings),
            "retro_covered_from": momentum_view.retro_history(session, settings).covered_from,
            "last_rebalance": momentum_view.last_rebalance(session),
            "last_run": views.last_run(session),
            "settings": settings,
        },
    )


@app.post("/run-now")
async def run_now(request: Request):
    """Synchroner Lauf, danach zurueck auf die Startseite."""
    if await _checked_form(request) is None:
        return _rejected(request, "token")
    try:
        result = await run_in_threadpool(run_daily, get_settings(), trigger="manual")
    except PipelineBusy:
        return RedirectResponse(url="/?notice=busy", status_code=303)
    notice = {"OK": "ok", "PARTIAL": "partial"}.get(result.status, "failed")
    return RedirectResponse(url=f"/?notice={notice}", status_code=303)


# -- JSON -------------------------------------------------------------------


@app.get("/api/signals")
def api_signals(
    status: str | None = Query(default=None, pattern="^(open|closed)$"),
    session: Session = Depends(get_session),
):
    if status == "open":
        data = views.open_signals(session)
    elif status == "closed":
        data = views.closed_signals(session)
    else:
        data = views.open_signals(session) + views.closed_signals(session)
    return JSONResponse([s.to_dict() for s in data])


@app.get("/api/earnings-events")
def api_events(
    limit: int = Query(default=200, ge=1, le=2000),
    session: Session = Depends(get_session),
):
    return JSONResponse(views.earnings_events(session, limit=limit))


@app.get("/api/tickers")
def api_tickers(session: Session = Depends(get_session)):
    return JSONResponse(ticker_view.ticker_overview(session, get_settings()))


@app.get("/api/momentum")
def api_momentum(
    status: str | None = Query(default=None, pattern="^(open|closed)$"),
    session: Session = Depends(get_session),
):
    if status == "open":
        data = momentum_view.open_positions(session)
    elif status == "closed":
        data = momentum_view.closed_positions(session)
    else:
        data = momentum_view.open_positions(session) + momentum_view.closed_positions(session)
    return JSONResponse(
        {
            "positions": [m.to_dict() for m in data],
            "summary": momentum_view.summary(session),
        }
    )


@app.get("/api/summary")
def api_summary(session: Session = Depends(get_session)):
    # Oberste Ebene wie bisher: die Live-Zahlen. Rueckwirkende getrennt daneben.
    data = views.summary(session)
    data["retro"] = views.summary(session, retro=True)
    return JSONResponse(data)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
