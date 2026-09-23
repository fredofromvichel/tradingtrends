"""Aufbereitung der DB-Zeilen fuer Frontend und JSON-API."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, asdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import market_context
from app.explain import Explanation, explain_pead
from app.market_context import MarketContext
from app.models import EarningsEvent, PipelineRun, Price, Signal
from app.signals import CLOSED, OPEN, trading_days_elapsed
from app.stats import MIN_SAMPLE, estimate_mean, estimate_rate


@dataclass
class SignalView:
    id: int
    symbol: str
    company: str | None
    industry: str | None
    exchange: str | None
    signal_type: str
    status: str
    trigger_date: str
    entry_date: str | None
    entry_price: float | None
    current_price: float | None
    current_price_date: str | None
    exit_date: str | None
    exit_price: float | None
    holding_period_days: int
    days_elapsed: int | None
    days_remaining: int | None
    sue: float | None
    surprise_pct: float | None
    return_pct: float | None
    benchmark_return_pct: float | None
    unrealized_pct: float | None
    # Anteil der Haltedauer, der verstrichen ist (0.0-1.0) - nur zur Anzeige.
    progress: float | None
    # Rueckwirkend berechnet (siehe models.RETRO_AFTER_DAYS) - zaehlt nicht
    # in die Live-Statistik.
    retro: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def tooltip(self) -> str:
        """Text fuer das Mouseover auf dem Symbol."""
        parts = [self.company or self.symbol]
        if self.industry:
            parts.append(self.industry)
        if self.exchange:
            parts.append(self.exchange)
        return " · ".join(parts)


def _build(
    session: Session, signal: Signal, context: MarketContext | None = None
) -> SignalView:
    # Ohne Kontext einen fuer dieses eine Symbol laden - bequem fuer
    # Einzelaufrufe, aber in Schleifen immer den geteilten Kontext uebergeben.
    context = context or market_context.load(session, [signal.symbol])

    current_price = context.close(signal.symbol)
    latest_date = context.last_date.get(signal.symbol)
    ticker = context.ticker(signal.symbol)

    days_elapsed = None
    days_remaining = None
    progress = None
    if signal.entry_date is not None:
        dates = context.dates(signal.symbol)
        days_elapsed = trading_days_elapsed(dates, signal.entry_date)
        days_remaining = max(0, signal.holding_period_days - days_elapsed)
        if signal.holding_period_days > 0:
            progress = min(1.0, days_elapsed / signal.holding_period_days)

    unrealized = None
    if signal.status == OPEN and signal.entry_price and current_price:
        raw = (current_price - signal.entry_price) / signal.entry_price
        unrealized = raw if signal.signal_type == "BUY" else -raw

    return SignalView(
        id=signal.id,
        symbol=signal.symbol,
        company=ticker.name if ticker else None,
        industry=ticker.industry if ticker else None,
        exchange=ticker.exchange if ticker else None,
        signal_type=signal.signal_type,
        status=signal.status,
        trigger_date=signal.trigger_date.isoformat(),
        entry_date=signal.entry_date.isoformat() if signal.entry_date else None,
        entry_price=signal.entry_price,
        current_price=current_price,
        current_price_date=latest_date.isoformat() if latest_date else None,
        exit_date=signal.exit_date.isoformat() if signal.exit_date else None,
        exit_price=signal.exit_price,
        holding_period_days=signal.holding_period_days,
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        sue=signal.sue_at_signal,
        surprise_pct=signal.surprise_pct_at_signal,
        return_pct=signal.return_pct,
        benchmark_return_pct=signal.benchmark_return_pct,
        unrealized_pct=unrealized,
        progress=progress,
        retro=bool(signal.retro),
    )


def open_signals(session: Session) -> list[SignalView]:
    rows = session.scalars(
        select(Signal).where(Signal.status == OPEN).order_by(Signal.trigger_date.desc())
    ).all()
    context = market_context.load(session, {s.symbol for s in rows})
    return [_build(session, s, context) for s in rows]


def closed_signals(session: Session, limit: int | None = None) -> list[SignalView]:
    query = select(Signal).where(Signal.status == CLOSED).order_by(Signal.exit_date.desc())
    if limit is not None:
        query = query.limit(limit)
    rows = session.scalars(query).all()
    context = market_context.load(session, {s.symbol for s in rows})
    return [_build(session, s, context) for s in rows]


def explanations_for(session: Session, views_: list, settings) -> dict[int, Explanation]:
    """Rechenwege zu den übergebenen Signalen, in einer Abfrage vorbereitet."""
    if not views_:
        return {}
    ids = [v.id for v in views_]
    signals = {
        s.id: s
        for s in session.scalars(select(Signal).where(Signal.id.in_(ids))).all()
    }
    event_ids = [s.earnings_event_id for s in signals.values()]
    events = {
        e.id: e
        for e in session.scalars(
            select(EarningsEvent).where(EarningsEvent.id.in_(event_ids))
        ).all()
    }
    return {
        sid: explain_pead(signal, events.get(signal.earnings_event_id), settings)
        for sid, signal in signals.items()
    }


def earnings_events(session: Session, limit: int = 200) -> list[dict]:
    rows = session.scalars(
        select(EarningsEvent).order_by(EarningsEvent.report_date.desc()).limit(limit)
    ).all()
    return [
        {
            "id": e.id,
            "symbol": e.symbol,
            "report_date": e.report_date.isoformat(),
            "source": e.source,
            "period": e.period,
            "report_hour": e.report_hour,
            "eps_estimate": e.eps_estimate,
            "eps_actual": e.eps_actual,
            "surprise_pct": e.surprise_pct,
            "sue": e.sue,
            "processed": e.processed,
            "ingested_at": e.ingested_at.isoformat() if e.ingested_at else None,
        }
        for e in rows
    ]


def last_run(session: Session) -> PipelineRun | None:
    return session.scalar(select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(1))


def summary(session: Session, retro: bool = False) -> dict:
    """Kennzahlen der geschlossenen Positionen, jeweils mit Unsicherheit.

    Getrennt nach Herkunft: ``retro=False`` (Standard) zaehlt nur Signale, die
    zum Zeitpunkt der Meldung entstanden sind - die belastbare Zahl.
    ``retro=True`` zaehlt die rueckwirkend berechneten; sie rechnen mit
    heutigen, nachtraeglich korrigierten Daten und sehen deshalb besser aus,
    als sie damals gewesen waeren.

    Punktschaetzer (Trefferquote, mittlere Rendite) bleiben leer, solange
    weniger als ``MIN_SAMPLE`` Positionen geschlossen sind. Die
    Konfidenzintervalle werden dagegen immer ausgewiesen - ihre Breite ist
    die eigentliche Aussage bei kleiner Stichprobe.
    """
    herkunft = Signal.retro if retro else ~Signal.retro
    closed = session.scalars(
        select(Signal).where(
            Signal.status == CLOSED, Signal.return_pct.isnot(None), herkunft
        )
    ).all()
    returns = [s.return_pct for s in closed if s.return_pct is not None]
    wins = sum(1 for r in returns if r > 0)
    beats = [
        s.return_pct - s.benchmark_return_pct
        for s in closed
        if s.return_pct is not None and s.benchmark_return_pct is not None
    ]

    rate = estimate_rate(wins, len(returns))
    mean = estimate_mean(returns)

    return {
        "origin": "retro" if retro else "live",
        "closed_count": len(returns),
        "win_count": wins,
        "min_sample": MIN_SAMPLE,
        "reliable": rate.reliable,
        "missing_for_reliable": max(0, MIN_SAMPLE - len(returns)),
        "win_rate": rate.point,
        "win_rate_low": rate.low,
        "win_rate_high": rate.high,
        "avg_return_pct": mean.mean,
        "avg_return_low": mean.low,
        "avg_return_high": mean.high,
        "median_return_pct": mean.median,
        "stdev_return_pct": mean.stdev,
        "best_return_pct": mean.maximum,
        "worst_return_pct": mean.minimum,
        "effect_distinguishable": mean.excludes_zero,
        # Wie oft das Signal besser lag als die uebrigen Titel im selben Fenster.
        "beat_count": sum(1 for b in beats if b > 0),
        "beat_base": len(beats),
        "open_count": session.scalar(
            select(func.count()).select_from(Signal).where(Signal.status == OPEN, herkunft)
        )
        or 0,
        "events_count": session.scalar(select(func.count()).select_from(EarningsEvent)) or 0,
    }
