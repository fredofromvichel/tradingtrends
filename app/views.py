"""Aufbereitung der DB-Zeilen fuer Frontend und JSON-API."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, asdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EarningsEvent, PipelineRun, Price, Signal
from app.signals import CLOSED, OPEN, trading_days_elapsed


@dataclass
class SignalView:
    id: int
    symbol: str
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
    unrealized_pct: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def _latest_price(session: Session, symbol: str) -> Price | None:
    return session.scalar(
        select(Price).where(Price.symbol == symbol).order_by(Price.date.desc()).limit(1)
    )


def _trading_dates(session: Session, symbol: str) -> list[dt.date]:
    return list(
        session.scalars(
            select(Price.date).where(Price.symbol == symbol).order_by(Price.date)
        ).all()
    )


def _build(session: Session, signal: Signal) -> SignalView:
    latest = _latest_price(session, signal.symbol)
    current_price = latest.close if latest else None

    days_elapsed = None
    days_remaining = None
    if signal.entry_date is not None:
        dates = _trading_dates(session, signal.symbol)
        days_elapsed = trading_days_elapsed(dates, signal.entry_date)
        days_remaining = max(0, signal.holding_period_days - days_elapsed)

    unrealized = None
    if signal.status == OPEN and signal.entry_price and current_price:
        raw = (current_price - signal.entry_price) / signal.entry_price
        unrealized = raw if signal.signal_type == "BUY" else -raw

    return SignalView(
        id=signal.id,
        symbol=signal.symbol,
        signal_type=signal.signal_type,
        status=signal.status,
        trigger_date=signal.trigger_date.isoformat(),
        entry_date=signal.entry_date.isoformat() if signal.entry_date else None,
        entry_price=signal.entry_price,
        current_price=current_price,
        current_price_date=latest.date.isoformat() if latest else None,
        exit_date=signal.exit_date.isoformat() if signal.exit_date else None,
        exit_price=signal.exit_price,
        holding_period_days=signal.holding_period_days,
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        sue=signal.sue_at_signal,
        surprise_pct=signal.surprise_pct_at_signal,
        return_pct=signal.return_pct,
        unrealized_pct=unrealized,
    )


def open_signals(session: Session) -> list[SignalView]:
    rows = session.scalars(
        select(Signal).where(Signal.status == OPEN).order_by(Signal.trigger_date.desc())
    ).all()
    return [_build(session, s) for s in rows]


def closed_signals(session: Session) -> list[SignalView]:
    rows = session.scalars(
        select(Signal).where(Signal.status == CLOSED).order_by(Signal.exit_date.desc())
    ).all()
    return [_build(session, s) for s in rows]


def earnings_events(session: Session, limit: int = 200) -> list[dict]:
    rows = session.scalars(
        select(EarningsEvent).order_by(EarningsEvent.report_date.desc()).limit(limit)
    ).all()
    return [
        {
            "id": e.id,
            "symbol": e.symbol,
            "report_date": e.report_date.isoformat(),
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


def summary(session: Session) -> dict:
    """Kennzahlen der geschlossenen Positionen - bewusst nuechtern gehalten."""
    closed = session.scalars(
        select(Signal).where(Signal.status == CLOSED, Signal.return_pct.isnot(None))
    ).all()
    returns = [s.return_pct for s in closed if s.return_pct is not None]
    wins = sum(1 for r in returns if r > 0)
    return {
        "closed_count": len(returns),
        "win_count": wins,
        "win_rate": (wins / len(returns)) if returns else None,
        "avg_return_pct": (sum(returns) / len(returns)) if returns else None,
        "best_return_pct": max(returns) if returns else None,
        "worst_return_pct": min(returns) if returns else None,
        "open_count": session.scalar(
            select(func.count()).select_from(Signal).where(Signal.status == OPEN)
        )
        or 0,
        "events_count": session.scalar(select(func.count()).select_from(EarningsEvent)) or 0,
    }
