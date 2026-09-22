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
from app.models import (
    AnalystRecommendation,
    EarningsEvent,
    MomentumRebalance,
    MomentumSignal,
    PipelineRun,
    Price,
    Signal,
    Ticker,
)
from app.momentum import momentum_score, rank_universe, required_history
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
    FinnhubForbiddenError,
    FinnhubTransientError,
    RawEarnings,
)
from app import coverage
from app.sources.earnings import FINNHUB, YAHOO
from app.sources import yahoo_earnings
from app.sources.prices import fetch_prices

log = logging.getLogger(__name__)

# Endpunktpfade - muessen zu denen im Finnhub-Client passen.
PROFILE_ENDPOINT = "/stock/profile2"
CALENDAR_ENDPOINT = "/calendar/earnings"
EARNINGS_ENDPOINT = "/stock/earnings"
RECOMMENDATION_ENDPOINT = "/stock/recommendation"

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
    momentum_opened: int = 0
    momentum_closed: int = 0
    status: str = "RUNNING"
    errors: list[str] = field(default_factory=list)
    # Hinweise ohne Fehlercharakter. Ein bekannter Tarif-Ausschluss darf den
    # Lauf nicht auf PARTIAL setzen - sonst hiesse PARTIAL dauerhaft "alles
    # normal" und echte Fehler gingen darin unter.
    notes: list[str] = field(default_factory=list)

    @property
    def message(self) -> str | None:
        return " | ".join(self.errors)[:2000] if self.errors else None

    @property
    def note(self) -> str | None:
        return " | ".join(self.notes)[:2000] if self.notes else None


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
        _step(result, "Ausblick", lambda: _ingest_outlook(settings))
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
        if settings.momentum.enabled:
            _step(result, "Momentum", lambda: _run_momentum(settings, result))

        with session_scope() as session:
            note = coverage.coverage_note(session)
        if note:
            result.notes.append(note)

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
        "pead=%d/%d momentum=%d/%d (eroeffnet/geschlossen)",
        result.status,
        result.events_ingested,
        result.prices_ingested,
        result.prices_fetched,
        result.signals_opened,
        result.signals_closed,
        result.momentum_opened,
        result.momentum_closed,
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
        run.momentum_opened = result.momentum_opened
        run.momentum_closed = result.momentum_closed
        run.prices_fetched = result.prices_fetched
        run.message = result.message
        run.notes = result.note


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
            with session_scope() as session:
                if coverage.should_skip(
                    session, symbol, PROFILE_ENDPOINT, settings.finnhub_recheck_days
                ):
                    continue

            try:
                profile = client.company_profile(symbol)
            except FinnhubAuthError as exc:
                raise RuntimeError(str(exc)) from exc
            except FinnhubForbiddenError as exc:
                with session_scope() as session:
                    coverage.record_block(session, symbol, exc.endpoint)
                continue
            except (FinnhubError, FinnhubTransientError) as exc:
                failures.append(f"{symbol} ({exc})")
                continue

            if profile.name is None:
                failures.append(f"{symbol} (kein Profil geliefert)")
                continue

            with session_scope() as session:
                coverage.record_success(session, symbol, PROFILE_ENDPOINT)
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
# Ausblick: naechster Meldetermin und Analystenbild
# ---------------------------------------------------------------------------


def _ingest_outlook(settings: Settings) -> None:
    """Holt den naechsten Earnings-Termin und die Analystenverteilung.

    Beides aendert sich langsam, deshalb nur, wenn der letzte Abruf laenger als
    ``outlook_max_age_days`` zurueckliegt. Das haelt den taeglichen Lauf
    schlank - sonst kaemen zwei zusaetzliche Requests je Ticker und Tag dazu.
    """
    today = dt.date.today()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=settings.outlook_max_age_days)

    with session_scope() as session:
        pending = [
            symbol
            for symbol, fetched_at, next_date in session.execute(
                select(Ticker.symbol, Ticker.outlook_fetched_at, Ticker.next_earnings_date)
                .where(Ticker.active.is_(True))
                .order_by(Ticker.symbol)
            ).all()
            # Neu abrufen, wenn nie geholt, zu alt, oder der gemerkte Termin
            # bereits verstrichen ist.
            if fetched_at is None
            or fetched_at.replace(tzinfo=dt.timezone.utc) < cutoff
            or (next_date is not None and next_date < today)
        ]

    if not pending:
        return

    log.info("Ausblick wird fuer %d Ticker aktualisiert.", len(pending))
    # Nach Symbol gruppiert, damit die Meldung Titel zaehlt und nicht
    # Fehlschlaege - zwei Endpunkte je Symbol ergaeben sonst die doppelte Zahl.
    failures: dict[str, list[str]] = {}
    with FinnhubClient(
        settings.finnhub_api_key,
        min_interval_seconds=settings.finnhub_min_interval_seconds,
    ) as client:
        for symbol in pending:
            next_date: dt.date | None = None

            with session_scope() as session:
                skip_calendar = coverage.should_skip(
                    session, symbol, CALENDAR_ENDPOINT, settings.finnhub_recheck_days
                )
            if not skip_calendar:
                try:
                    upcoming = client.earnings_calendar(
                        symbol, today, today + dt.timedelta(days=120)
                    )
                    future = sorted(
                        e.report_date for e in upcoming if e.report_date >= today
                    )
                    next_date = future[0] if future else None
                    with session_scope() as session:
                        coverage.record_success(session, symbol, CALENDAR_ENDPOINT)
                except FinnhubAuthError as exc:
                    raise RuntimeError(str(exc)) from exc
                except FinnhubForbiddenError as exc:
                    with session_scope() as session:
                        coverage.record_block(session, symbol, exc.endpoint)
                except (FinnhubError, FinnhubTransientError) as exc:
                    failures.setdefault(symbol, []).append(f"Termin ({exc})")

            with session_scope() as session:
                skip_reco = coverage.should_skip(
                    session, symbol, RECOMMENDATION_ENDPOINT, settings.finnhub_recheck_days
                )
            if not skip_reco:
                try:
                    for reco in client.recommendations(symbol)[:12]:
                        _store_recommendation(reco)
                    with session_scope() as session:
                        coverage.record_success(session, symbol, RECOMMENDATION_ENDPOINT)
                except FinnhubAuthError as exc:
                    raise RuntimeError(str(exc)) from exc
                except FinnhubForbiddenError as exc:
                    with session_scope() as session:
                        coverage.record_block(session, symbol, exc.endpoint)
                except (FinnhubError, FinnhubTransientError) as exc:
                    failures.setdefault(symbol, []).append(f"Empfehlungen ({exc})")

            if next_date is None and settings.earnings_fallback_enabled:
                try:
                    next_date = yahoo_earnings.fetch_next_earnings_date(symbol)
                except yahoo_earnings.YahooEarningsError as exc:
                    log.debug("Termin ueber Ausweichquelle fuer %s: %s", symbol, exc)

            with session_scope() as session:
                row = session.get(Ticker, symbol)
                if row is not None:
                    if next_date is not None:
                        row.next_earnings_date = next_date
                    row.outlook_fetched_at = dt.datetime.now(dt.timezone.utc)

    if failures:
        details = "; ".join(
            f"{symbol}: {', '.join(gruende)}"
            for symbol, gruende in list(failures.items())[:4]
        )
        raise RuntimeError(
            f"Ausblick fuer {len(failures)} Symbol(e) unvollstaendig: {details}"
        )


def _store_recommendation(reco) -> None:
    with session_scope() as session:
        existing = session.scalar(
            select(AnalystRecommendation).where(
                AnalystRecommendation.symbol == reco.symbol,
                AnalystRecommendation.period == reco.period,
            )
        )
        target = existing or AnalystRecommendation(
            symbol=reco.symbol, period=reco.period
        )
        target.strong_buy = reco.strong_buy
        target.buy = reco.buy
        target.hold = reco.hold
        target.sell = reco.sell
        target.strong_sell = reco.strong_sell
        target.fetched_at = dt.datetime.now(dt.timezone.utc)
        if existing is None:
            session.add(target)


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

    failures: dict[str, list[str]] = {}
    fallback_used: set[str] = set()
    with FinnhubClient(
        settings.finnhub_api_key,
        min_interval_seconds=settings.finnhub_min_interval_seconds,
    ) as client:
        for symbol in symbols:
            with session_scope() as session:
                gesperrt = coverage.should_skip(
                    session, symbol, EARNINGS_ENDPOINT, settings.finnhub_recheck_days
                ) and coverage.should_skip(
                    session, symbol, CALENDAR_ENDPOINT, settings.finnhub_recheck_days
                )
            if gesperrt:
                # Finnhub ist fuer diesen Titel gesperrt - direkt ausweichen,
                # ohne den aussichtslosen Versuch.
                if not settings.earnings_fallback_enabled:
                    continue
                try:
                    yahoo = yahoo_earnings.fetch_earnings(symbol)
                except yahoo_earnings.YahooEarningsError as exc:
                    failures.setdefault(symbol, []).append(f"Ausweichquelle ({exc})")
                    continue
                if yahoo:
                    fallback_used.add(symbol)
                    for raw in yahoo:
                        if not (date_from <= raw.report_date <= date_to):
                            continue
                        if _store_event(settings, raw, yahoo):
                            result.events_ingested += 1
                continue

            history: list[RawEarnings] = []
            finnhub_blocked = False
            try:
                history = client.earnings_surprises(symbol)
                with session_scope() as session:
                    coverage.record_success(session, symbol, EARNINGS_ENDPOINT)
            except FinnhubAuthError as exc:
                # Betrifft jedes Symbol gleichermassen - weitere Requests
                # waeren nur verschwendete Zeit.
                raise RuntimeError(str(exc)) from exc
            except FinnhubForbiddenError as exc:
                finnhub_blocked = True
                with session_scope() as session:
                    coverage.record_block(session, symbol, exc.endpoint)
            except (FinnhubError, FinnhubTransientError) as exc:
                failures.setdefault(symbol, []).append(f"Historie ({exc})")

            try:
                events = client.earnings_calendar(symbol, date_from, date_to)
                with session_scope() as session:
                    coverage.record_success(session, symbol, CALENDAR_ENDPOINT)
            except FinnhubAuthError as exc:
                raise RuntimeError(str(exc)) from exc
            except FinnhubForbiddenError as exc:
                finnhub_blocked = True
                with session_scope() as session:
                    coverage.record_block(session, symbol, exc.endpoint)
                events = []
            except FinnhubTransientError as exc:
                failures.setdefault(symbol, []).append(f"Kalender ({exc})")
                continue

            # Ausweichquelle: liefert Meldedatum und Historie in einem Abruf.
            if finnhub_blocked and settings.earnings_fallback_enabled:
                try:
                    yahoo = yahoo_earnings.fetch_earnings(symbol)
                except yahoo_earnings.YahooEarningsError as exc:
                    failures.setdefault(symbol, []).append(f"Ausweichquelle ({exc})")
                    yahoo = []
                if yahoo:
                    history = yahoo
                    events = [e for e in yahoo if date_from <= e.report_date <= date_to]
                    fallback_used.add(symbol)

            for raw in events:
                if raw.eps_actual is None:
                    # Noch nicht gemeldet - erst ein kuenftiger Kalendereintrag.
                    continue
                if _store_event(settings, raw, history):
                    result.events_ingested += 1

    if fallback_used:
        log.info(
            "Earnings ueber die Ausweichquelle fuer %d Titel: %s",
            len(fallback_used),
            ", ".join(sorted(fallback_used)),
        )

    if failures:
        details = "; ".join(
            f"{symbol}: {', '.join(gruende)}"
            for symbol, gruende in list(failures.items())[:4]
        )
        raise RuntimeError(f"{len(failures)} Symbol(e) fehlerhaft: {details}")


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
                source=raw.source,
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
# Zweite Signalquelle: Cross-Sectional-Momentum
# ---------------------------------------------------------------------------


def _period_key(day: dt.date, cadence: str) -> str:
    """Schluessel der Umschichtungsperiode - macht den Lauf idempotent."""
    if cadence != "monthly":
        raise ValueError(f"Unbekannte Umschichtungsfrequenz: {cadence}")
    return f"{day.year:04d}-{day.month:02d}"


def _run_momentum(settings: Settings, result: RunResult) -> None:
    """Schliesst faellige Momentum-Positionen und schichtet bei Bedarf um."""
    _close_momentum_positions(settings, result)
    _rebalance_momentum(settings, result)


def _close_momentum_positions(settings: Settings, result: RunResult) -> None:
    with session_scope() as session:
        for signal in session.scalars(
            select(MomentumSignal).where(MomentumSignal.status == OPEN)
        ).all():
            dates = _trading_dates(session, signal.symbol)
            if not dates:
                continue

            if signal.entry_price is None:
                entry_day = find_entry_day(dates, signal.entry_date or dates[0], "bmo")
                if entry_day is None:
                    continue
                price = session.get(Price, {"symbol": signal.symbol, "date": entry_day})
                if price is None:
                    continue
                signal.entry_date = entry_day
                signal.entry_price = price.close

            if signal.entry_date is None or signal.entry_price is None:
                continue

            exit_day = find_exit_day(dates, signal.entry_date, signal.holding_period_days)
            if exit_day is None:
                continue
            exit_row = session.get(Price, {"symbol": signal.symbol, "date": exit_day})
            if exit_row is None:
                continue

            signal.exit_date = exit_day
            signal.exit_price = exit_row.close
            signal.status = CLOSED
            # LONG rechnet wie BUY, SHORT wie SELL.
            signal.return_pct = compute_return_pct(
                "BUY" if signal.direction == "LONG" else "SELL",
                signal.entry_price,
                exit_row.close,
            )
            result.momentum_closed += 1
            log.info(
                "Momentum geschlossen: %s %s -> %.2f%%",
                signal.symbol,
                signal.direction,
                signal.return_pct * 100,
            )


def _rebalance_momentum(settings: Settings, result: RunResult) -> None:
    cfg = settings.momentum
    today = dt.date.today()
    period = _period_key(today, cfg.rebalance)

    with session_scope() as session:
        if session.scalar(
            select(MomentumRebalance).where(MomentumRebalance.period_key == period)
        ):
            return  # in dieser Periode bereits umgeschichtet

        symbols = _active_symbols(session)
        scores: dict[str, float | None] = {}
        formation_start: dt.date | None = None
        formation_end: dt.date | None = None
        short_history: list[str] = []

        for symbol in symbols:
            closes_rows = session.execute(
                select(Price.date, Price.close)
                .where(Price.symbol == symbol)
                .order_by(Price.date)
            ).all()
            closes = [row[1] for row in closes_rows]
            score = momentum_score(closes, cfg.lookback_days, cfg.skip_days)
            scores[symbol] = score
            if score is None:
                short_history.append(symbol)
                continue
            start_row = closes_rows[-(cfg.lookback_days + 1)]
            end_row = closes_rows[-(cfg.skip_days + 1)]
            formation_start = start_row[0] if formation_start is None else min(
                formation_start, start_row[0]
            )
            formation_end = end_row[0] if formation_end is None else max(
                formation_end, end_row[0]
            )

        basket = rank_universe(scores, cfg.group_fraction, cfg.min_universe)

        if basket.is_empty:
            log.info("Momentum: keine Umschichtung - %s", basket.reason)
            if short_history:
                raise RuntimeError(
                    f"Momentum uebersprungen: {basket.reason} Zu kurze Historie bei "
                    f"{', '.join(short_history[:8])}. Kurse mit 'make backfill' "
                    f"nachladen (noetig: {required_history(cfg.lookback_days)} Kurstage)."
                )
            return

        rebalance = MomentumRebalance(
            period_key=period,
            rebalance_date=today,
            formation_start=formation_start,
            formation_end=formation_end,
            universe_size=basket.universe_size,
            group_size=basket.group_size,
            lookback_days=cfg.lookback_days,
            skip_days=cfg.skip_days,
        )
        session.add(rebalance)
        session.flush()

        for ranked in basket.longs + basket.shorts:
            dates = _trading_dates(session, ranked.symbol)
            entry_day = find_entry_day(dates, today, "bmo") if dates else None
            if entry_day is None and dates:
                entry_day = dates[-1]   # noch kein Kurs von heute: letzter bekannter
            entry_price = None
            if entry_day is not None:
                price = session.get(Price, {"symbol": ranked.symbol, "date": entry_day})
                entry_price = price.close if price else None

            session.add(
                MomentumSignal(
                    symbol=ranked.symbol,
                    rebalance_id=rebalance.id,
                    direction=ranked.direction,
                    rank=ranked.rank,
                    momentum_score=ranked.score,
                    entry_date=entry_day if entry_price is not None else None,
                    entry_price=entry_price,
                    holding_period_days=cfg.holding_period_days,
                    status=OPEN,
                )
            )
            result.momentum_opened += 1

        log.info(
            "Momentum umgeschichtet (%s): %d long, %d short aus %d Titeln. "
            "Spitze: %s | Schluss: %s",
            period,
            len(basket.longs),
            len(basket.shorts),
            basket.universe_size,
            ", ".join(f"{r.symbol} {r.score:+.1%}" for r in basket.longs),
            ", ".join(f"{r.symbol} {r.score:+.1%}" for r in basket.shorts),
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
