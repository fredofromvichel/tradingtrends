"""Taeglicher Pipeline-Lauf: Earnings -> Kurse -> Positionen -> Signale."""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import session_scope, sync_tickers
from app.models import EarningsEvent, PipelineRun, Price, Signal, Ticker
from app.signals import (
    CLOSED,
    OPEN,
    compute_return_pct,
    compute_sue,
    compute_surprise_pct,
    decide,
    find_entry_day,
    find_exit_day,
)
from app.sources.finnhub_client import (
    FinnhubAuthError,
    FinnhubClient,
    FinnhubError,
    FinnhubTransientError,
    RawEarnings,
)
from app.sources.prices import fetch_prices

log = logging.getLogger(__name__)

# Nur ein Lauf gleichzeitig - Scheduler und "Jetzt aktualisieren" teilen sich
# den Prozess.
_run_lock = threading.Lock()


class PipelineBusy(RuntimeError):
    """Ein Lauf ist bereits aktiv."""


@dataclass
class RunResult:
    trigger: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    events_ingested: int = 0
    prices_ingested: int = 0
    # Nur zur Anzeige, nicht persistiert: wie viele Kurszeilen die Quelle
    # geliefert hat. Bei einem Wiederholungslauf sind fast alle davon schon
    # bekannt, prices_ingested ist dann 0, obwohl der Abruf funktioniert hat.
    prices_fetched: int = 0
    signals_opened: int = 0
    signals_closed: int = 0
    status: str = "RUNNING"
    errors: list[str] = field(default_factory=list)

    @property
    def message(self) -> str | None:
        return " | ".join(self.errors)[:2000] if self.errors else None


def run_daily(
    settings: Settings,
    trigger: str = "manual",
    *,
    force_price_backfill: bool = False,
) -> RunResult:
    """Fuehrt den kompletten Tageslauf aus.

    Die einzelnen Schritte sind gegeneinander abgeschottet: faellt Finnhub aus,
    laufen Kursaktualisierung und Positionspflege trotzdem durch (und umgekehrt).
    Der Lauf gilt als PARTIAL, wenn mindestens ein Schritt scheiterte.

    ``force_price_backfill`` holt die volle Kurshistorie statt nur des kurzen
    Aktualisierungsfensters - noetig, wenn nachtraeglich weit zurueckliegende
    Earnings eingelesen werden (siehe ``app.cli seed``).
    """
    if not _run_lock.acquire(blocking=False):
        raise PipelineBusy("Es laeuft bereits ein Pipeline-Durchlauf.")

    result = RunResult(trigger=trigger, started_at=dt.datetime.now(dt.timezone.utc))
    run_id: int | None = None
    try:
        with session_scope() as session:
            run = PipelineRun(started_at=result.started_at, trigger=trigger, status="RUNNING")
            session.add(run)
            session.flush()
            run_id = run.id
            sync_tickers(session, settings.tickers)

        log.info("Pipeline-Lauf gestartet (trigger=%s, run_id=%s)", trigger, run_id)

        _step(result, "Stammdaten", lambda: _ingest_profiles(settings))
        _step(result, "Earnings-Abruf", lambda: _ingest_earnings(settings, result))
        _step(
            result,
            "Kurs-Abruf",
            lambda: _ingest_prices(settings, result, force_backfill=force_price_backfill),
        )
        # Signale VOR der Positionspflege: im Tagesbetrieb aenderte die
        # Reihenfolge nichts (ein heute eroeffnetes Signal kann heute nicht
        # faellig sein), aber beim Nachladen alter Earnings wird eine laengst
        # abgelaufene Position so im selben Lauf geschlossen statt erst im
        # naechsten. Ausserdem traegt die Positionspflege einen fehlenden
        # Einstiegskurs direkt nach.
        _step(result, "Signal-Erzeugung", lambda: _create_signals(settings, result))
        _step(result, "Positionspflege", lambda: _update_positions(settings, result))

        result.status = "PARTIAL" if result.errors else "OK"
    except Exception as exc:  # pragma: no cover - Sicherheitsnetz
        log.exception("Pipeline-Lauf abgebrochen")
        result.status = "FAILED"
        result.errors.append(f"Abbruch: {exc}")
    finally:
        result.finished_at = dt.datetime.now(dt.timezone.utc)
        if run_id is not None:
            try:
                _persist_run(run_id, result)
            except Exception:  # pragma: no cover
                log.exception("Lauf-Protokoll konnte nicht geschrieben werden")
        _run_lock.release()

    log.info(
        "Pipeline-Lauf beendet: status=%s events=%d kurse=%d neu (%d abgerufen) "
        "eroeffnet=%d geschlossen=%d",
        result.status,
        result.events_ingested,
        result.prices_ingested,
        result.prices_fetched,
        result.signals_opened,
        result.signals_closed,
    )
    return result


def _step(result: RunResult, label: str, func) -> None:
    try:
        func()
    except Exception as exc:
        # Eine lesbare Zeile im Normalbetrieb; der vollstaendige Stacktrace
        # steht bei LOG_LEVEL=DEBUG zur Verfuegung.
        log.error("Schritt '%s' fehlgeschlagen: %s", label, exc)
        log.debug("Stacktrace zu '%s'", label, exc_info=True)
        result.errors.append(f"{label}: {exc}")


def _persist_run(run_id: int, result: RunResult) -> None:
    with session_scope() as session:
        run = session.get(PipelineRun, run_id)
        if run is None:
            return
        run.finished_at = result.finished_at
        run.status = result.status
        run.events_ingested = result.events_ingested
        run.prices_ingested = result.prices_ingested
        run.signals_opened = result.signals_opened
        run.signals_closed = result.signals_closed
        run.message = result.message


# ---------------------------------------------------------------------------
# Stammdaten: einmalig je Ticker
# ---------------------------------------------------------------------------


def _ingest_profiles(settings: Settings) -> None:
    """Holt Firmenname und Branche fuer Ticker, bei denen sie noch fehlen.

    Laeuft praktisch nur beim ersten Durchgang: sobald ein Name in der
    Datenbank steht, wird das Symbol uebersprungen. Ein fehlendes Profil ist
    kein Grund, den Lauf zu gefaehrden - die Anzeige faellt dann auf das
    Symbol zurueck.
    """
    with session_scope() as session:
        pending = list(
            session.scalars(
                select(Ticker.symbol).where(
                    Ticker.active.is_(True), Ticker.name.is_(None)
                )
            ).all()
        )
    if not pending:
        return

    log.info("Stammdaten fehlen fuer %d Ticker - werden nachgeladen.", len(pending))
    failures: list[str] = []
    with FinnhubClient(
        settings.finnhub_api_key,
        min_interval_seconds=settings.finnhub_min_interval_seconds,
    ) as client:
        for symbol in pending:
            try:
                profile = client.company_profile(symbol)
            except FinnhubAuthError as exc:
                raise RuntimeError(str(exc)) from exc
            except (FinnhubError, FinnhubTransientError) as exc:
                failures.append(f"{symbol} ({exc})")
                continue

            if profile.name is None:
                failures.append(f"{symbol} (kein Profil geliefert)")
                continue

            with session_scope() as session:
                row = session.get(Ticker, symbol)
                if row is not None:
                    row.name = profile.name
                    row.industry = profile.industry
                    row.exchange = profile.exchange
            log.info("Stammdaten: %s = %s (%s)", symbol, profile.name, profile.industry)

    if failures:
        raise RuntimeError(
            f"Stammdaten fuer {len(failures)} Symbol(e) nicht abrufbar: "
            f"{'; '.join(failures[:5])}"
        )


# ---------------------------------------------------------------------------
# Schritt 1+2: Earnings abrufen und neue Events verarbeiten
# ---------------------------------------------------------------------------


def _ingest_earnings(settings: Settings, result: RunResult) -> None:
    today = dt.date.today()
    date_from = today - dt.timedelta(days=settings.lookback_days_earnings)
    # Ein Tag Vorlauf: Meldungen nach US-Boersenschluss tragen in Finnhub
    # teils schon das Datum des Folgetags.
    date_to = today + dt.timedelta(days=1)

    with session_scope() as session:
        symbols = _active_symbols(session)

    failures: list[str] = []
    with FinnhubClient(
        settings.finnhub_api_key,
        min_interval_seconds=settings.finnhub_min_interval_seconds,
    ) as client:
        for symbol in symbols:
            try:
                history = client.earnings_surprises(symbol)
            except FinnhubAuthError as exc:
                # Betrifft jedes Symbol gleichermassen - weitere Requests
                # waeren nur verschwendete Zeit.
                raise RuntimeError(str(exc)) from exc
            except (FinnhubError, FinnhubTransientError) as exc:
                failures.append(f"{symbol}: Historie ({exc})")
                history = []

            try:
                events = client.earnings_calendar(symbol, date_from, date_to)
            except FinnhubAuthError as exc:
                raise RuntimeError(str(exc)) from exc
            except FinnhubError as exc:
                # Kalender im Tarif gesperrt -> Naeherung ueber die Historie.
                log.warning("Earnings-Kalender fuer %s nicht verfuegbar: %s", symbol, exc)
                events = [e for e in history if date_from <= e.report_date <= date_to]
                if events:
                    log.warning(
                        "%s: nutze Fiskalperiode als Naeherung fuer das Meldedatum.", symbol
                    )
            except FinnhubTransientError as exc:
                failures.append(f"{symbol}: Kalender ({exc})")
                continue

            for raw in events:
                if raw.eps_actual is None:
                    # Noch nicht gemeldet - erst ein kuenftiger Kalendereintrag.
                    continue
                if _store_event(settings, raw, history):
                    result.events_ingested += 1

    if failures:
        raise RuntimeError(f"{len(failures)} Symbol(e) fehlerhaft: {'; '.join(failures[:5])}")


def _store_event(settings: Settings, raw: RawEarnings, history: list[RawEarnings]) -> bool:
    """Legt ein Earnings-Event an bzw. ergaenzt es. True, wenn neu."""
    surprise_pct = compute_surprise_pct(raw.eps_actual, raw.eps_estimate, raw.surprise_percent)

    # Historie ohne das aktuelle Quartal.
    hist_values: list[float] = []
    for item in history:
        if item.report_date >= raw.report_date:
            continue
        value = compute_surprise_pct(item.eps_actual, item.eps_estimate, item.surprise_percent)
        if value is not None:
            hist_values.append(value)

    sue = compute_sue(surprise_pct, hist_values, settings.min_history_for_sue)

    with session_scope() as session:
        existing = session.scalar(
            select(EarningsEvent).where(
                EarningsEvent.symbol == raw.symbol,
                EarningsEvent.report_date == raw.report_date,
            )
        )
        if existing is not None:
            # Nachmeldungen (z. B. epsActual erst spaeter befuellt) uebernehmen,
            # solange noch kein Signal daraus entstanden ist.
            if not existing.processed:
                existing.eps_actual = raw.eps_actual
                existing.eps_estimate = raw.eps_estimate
                existing.surprise_pct = surprise_pct
                existing.sue = sue
            return False

        session.add(
            EarningsEvent(
                symbol=raw.symbol,
                report_date=raw.report_date,
                period=raw.period,
                report_hour=raw.hour,
                eps_estimate=raw.eps_estimate,
                eps_actual=raw.eps_actual,
                surprise_pct=surprise_pct,
                sue=sue,
                processed=False,
            )
        )
    log.info(
        "Neues Earnings-Event: %s am %s (surprise=%s, sue=%s)",
        raw.symbol,
        raw.report_date,
        _fmt(surprise_pct),
        _fmt(sue),
    )
    return True


# ---------------------------------------------------------------------------
# Schritt 4: Kursdaten
# ---------------------------------------------------------------------------


def _ingest_prices(
    settings: Settings, result: RunResult, *, force_backfill: bool = False
) -> None:
    today = dt.date.today()
    with session_scope() as session:
        symbols = _active_symbols(session)
        has_prices = session.scalar(select(Price.symbol).limit(1)) is not None

    use_short_window = has_prices and not force_backfill
    window = settings.price_refresh_days if use_short_window else settings.price_backfill_days
    start = today - dt.timedelta(days=window)
    log.info("Kursabruf fuer %d Symbole ab %s", len(symbols), start)

    rows, missing = fetch_prices(list(symbols), start, today)
    result.prices_fetched += len(rows)

    written = 0
    with session_scope() as session:
        for row in rows:
            existing = session.get(Price, {"symbol": row.symbol, "date": row.date})
            if existing is None:
                session.add(
                    Price(
                        symbol=row.symbol,
                        date=row.date,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        volume=row.volume,
                    )
                )
                written += 1
            else:
                # Bereinigungen (Splits/Dividenden) aktualisieren Altbestaende.
                existing.open = row.open
                existing.high = row.high
                existing.low = row.low
                existing.close = row.close
                existing.volume = row.volume

    result.prices_ingested += written
    if missing:
        raise RuntimeError(f"Keine Kursdaten fuer: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Schritt 5: offene Positionen pruefen
# ---------------------------------------------------------------------------


def _update_positions(settings: Settings, result: RunResult) -> None:
    with session_scope() as session:
        open_signals = list(
            session.scalars(select(Signal).where(Signal.status == OPEN)).all()
        )
        for signal in open_signals:
            dates = _trading_dates(session, signal.symbol)
            if not dates:
                continue

            # Einstieg nachtragen, falls beim Anlegen noch kein Kurs vorlag.
            if signal.entry_price is None:
                event = session.get(EarningsEvent, signal.earnings_event_id)
                entry_day = find_entry_day(
                    dates, signal.trigger_date, event.report_hour if event else None
                )
                if entry_day is None:
                    continue
                price = session.get(Price, {"symbol": signal.symbol, "date": entry_day})
                if price is None:
                    continue
                signal.entry_date = entry_day
                signal.entry_price = price.close
                log.info(
                    "Einstieg nachgetragen: %s #%s am %s zu %.2f",
                    signal.symbol,
                    signal.id,
                    entry_day,
                    price.close,
                )

            if signal.entry_date is None or signal.entry_price is None:
                continue

            exit_day = find_exit_day(dates, signal.entry_date, signal.holding_period_days)
            if exit_day is None:
                continue
            exit_price_row = session.get(Price, {"symbol": signal.symbol, "date": exit_day})
            if exit_price_row is None:
                continue

            signal.exit_date = exit_day
            signal.exit_price = exit_price_row.close
            signal.status = CLOSED
            signal.return_pct = compute_return_pct(
                signal.signal_type, signal.entry_price, exit_price_row.close
            )
            result.signals_closed += 1
            log.info(
                "Position geschlossen: %s #%s %s -> %.2f%% (Einstieg %.2f, Ausstieg %.2f)",
                signal.symbol,
                signal.id,
                signal.signal_type,
                signal.return_pct * 100,
                signal.entry_price,
                exit_price_row.close,
            )


# ---------------------------------------------------------------------------
# Schritt 6: neue Signale anlegen
# ---------------------------------------------------------------------------


def _create_signals(settings: Settings, result: RunResult) -> None:
    with session_scope() as session:
        pending = list(
            session.scalars(
                select(EarningsEvent)
                .where(EarningsEvent.processed.is_(False))
                .order_by(EarningsEvent.report_date)
            ).all()
        )

        for event in pending:
            decision = decide(
                event.surprise_pct,
                event.sue,
                sue_threshold_buy=settings.sue_threshold_buy,
                sue_threshold_sell=settings.sue_threshold_sell,
                surprise_pct_fallback_buy=settings.surprise_pct_fallback_buy,
                surprise_pct_fallback_sell=settings.surprise_pct_fallback_sell,
            )

            if decision.signal_type is None:
                event.processed = True
                log.info(
                    "Kein Signal fuer %s (%s): %s",
                    event.symbol,
                    event.report_date,
                    decision.reason,
                )
                continue

            already = session.scalar(
                select(Signal).where(Signal.earnings_event_id == event.id)
            )
            if already is not None:
                event.processed = True
                continue

            dates = _trading_dates(session, event.symbol)
            entry_day = find_entry_day(dates, event.report_date, event.report_hour)
            entry_price = None
            if entry_day is not None:
                price = session.get(Price, {"symbol": event.symbol, "date": entry_day})
                entry_price = price.close if price else None
            if entry_price is None:
                # Kein Kurs am/ab Meldetag - Signal trotzdem anlegen, der
                # naechste Lauf traegt den Einstieg nach.
                entry_day = None
                log.info(
                    "%s: Einstiegskurs fuer %s noch nicht verfuegbar, wird nachgetragen.",
                    event.symbol,
                    event.report_date,
                )

            session.add(
                Signal(
                    symbol=event.symbol,
                    earnings_event_id=event.id,
                    signal_type=decision.signal_type,
                    trigger_date=event.report_date,
                    entry_date=entry_day,
                    entry_price=entry_price,
                    holding_period_days=settings.holding_period_days,
                    status=OPEN,
                    sue_at_signal=event.sue,
                    surprise_pct_at_signal=event.surprise_pct,
                )
            )
            event.processed = True
            result.signals_opened += 1
            log.info(
                "Signal eroeffnet: %s %s am %s (%s)",
                decision.signal_type,
                event.symbol,
                event.report_date,
                decision.reason,
            )


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def _active_symbols(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(Ticker.symbol).where(Ticker.active.is_(True)).order_by(Ticker.symbol)
        ).all()
    )


def _trading_dates(session: Session, symbol: str) -> list[dt.date]:
    return list(
        session.scalars(
            select(Price.date).where(Price.symbol == symbol).order_by(Price.date)
        ).all()
    )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"
