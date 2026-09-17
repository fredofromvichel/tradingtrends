"""FastAPI-Anwendung: HTML-Views, JSON-API, Scheduler."""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import views
from app.config import Settings, load_settings
from app.db import get_sessionmaker, init_engine, session_scope, sync_tickers
from app.logging_conf import configure_logging
from app.pipeline import PipelineBusy, run_daily
from app.scheduler import start_scheduler

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

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
    log.info("PEAD-Signal-POC startet - %d Ticker konfiguriert.", len(settings.tickers))

    init_engine(settings.db_path)
    with session_scope() as session:
        sync_tickers(session, settings.tickers)

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


# -- Jinja-Filter -----------------------------------------------------------


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"{value * 100:+.{digits}f} %"


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
templates.env.filters["num"] = _fmt_num
templates.env.filters["sue"] = _fmt_sue
templates.env.filters["dtfmt"] = _fmt_dt


# -- HTML -------------------------------------------------------------------


@app.get("/", response_class=None)
def index(request: Request, session: Session = Depends(get_session)):
    settings = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "signals": views.open_signals(session),
            "summary": views.summary(session),
            "last_run": views.last_run(session),
            "settings": settings,
            "ticker_count": len(settings.tickers),
            "notice": request.query_params.get("notice"),
        },
    )


@app.get("/history", response_class=None)
def history(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "signals": views.closed_signals(session),
            "summary": views.summary(session),
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
            "last_run": views.last_run(session),
            "settings": get_settings(),
        },
    )


@app.post("/run-now")
def run_now():
    """Synchroner Lauf, danach zurueck auf die Startseite."""
    try:
        result = run_daily(get_settings(), trigger="manual")
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


@app.get("/api/summary")
def api_summary(session: Session = Depends(get_session)):
    return JSONResponse(views.summary(session))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
