"""Aufbereitung der Momentum-Seite.

Bewusst eigene Datei neben views.py: die beiden Signalquellen werden getrennt
ausgewiesen. Die Kennzahlen laufen durch dieselbe Statistik wie bei PEAD -
Punktschaetzer erst ab MIN_SAMPLE, Konfidenzintervall immer.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import MomentumRebalance, MomentumSignal, Price, Ticker
from app.momentum import momentum_score, required_history
from app.signals import CLOSED, OPEN, trading_days_elapsed
from app.stats import MIN_SAMPLE, estimate_mean, estimate_rate


@dataclass
class MomentumView:
    id: int
    symbol: str
    company: str | None
    direction: str
    rank: int
    momentum_score: float
    entry_date: str | None
    entry_price: float | None
    current_price: float | None
    exit_date: str | None
    exit_price: float | None
    holding_period_days: int
    days_remaining: int | None
    progress: float | None
    status: str
    return_pct: float | None
    unrealized_pct: float | None
    rebalance_date: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _latest_close(session: Session, symbol: str) -> float | None:
    row = session.scalar(
        select(Price).where(Price.symbol == symbol).order_by(Price.date.desc()).limit(1)
    )
    return row.close if row else None


def _trading_dates(session: Session, symbol: str) -> list[dt.date]:
    return list(
        session.scalars(
            select(Price.date).where(Price.symbol == symbol).order_by(Price.date)
        ).all()
    )


def _build(session: Session, signal: MomentumSignal) -> MomentumView:
    ticker = session.get(Ticker, signal.symbol)
    rebalance = session.get(MomentumRebalance, signal.rebalance_id)
    current = _latest_close(session, signal.symbol)

    days_remaining = None
    progress = None
    if signal.entry_date is not None:
        elapsed = trading_days_elapsed(_trading_dates(session, signal.symbol), signal.entry_date)
        days_remaining = max(0, signal.holding_period_days - elapsed)
        if signal.holding_period_days > 0:
            progress = min(1.0, elapsed / signal.holding_period_days)

    unrealized = None
    if signal.status == OPEN and signal.entry_price and current:
        raw = (current - signal.entry_price) / signal.entry_price
        unrealized = raw if signal.direction == "LONG" else -raw

    return MomentumView(
        id=signal.id,
        symbol=signal.symbol,
        company=ticker.name if ticker else None,
        direction=signal.direction,
        rank=signal.rank,
        momentum_score=signal.momentum_score,
        entry_date=signal.entry_date.isoformat() if signal.entry_date else None,
        entry_price=signal.entry_price,
        current_price=current,
        exit_date=signal.exit_date.isoformat() if signal.exit_date else None,
        exit_price=signal.exit_price,
        holding_period_days=signal.holding_period_days,
        days_remaining=days_remaining,
        progress=progress,
        status=signal.status,
        return_pct=signal.return_pct,
        unrealized_pct=unrealized,
        rebalance_date=rebalance.rebalance_date.isoformat() if rebalance else None,
    )


def open_positions(session: Session) -> list[MomentumView]:
    rows = session.scalars(
        select(MomentumSignal)
        .where(MomentumSignal.status == OPEN)
        .order_by(MomentumSignal.direction, MomentumSignal.rank)
    ).all()
    return [_build(session, s) for s in rows]


def closed_positions(session: Session, limit: int = 200) -> list[MomentumView]:
    rows = session.scalars(
        select(MomentumSignal)
        .where(MomentumSignal.status == CLOSED)
        .order_by(MomentumSignal.exit_date.desc())
        .limit(limit)
    ).all()
    return [_build(session, s) for s in rows]


def for_symbol(session: Session, symbol: str) -> list[MomentumView]:
    rows = session.scalars(
        select(MomentumSignal)
        .where(MomentumSignal.symbol == symbol)
        .order_by(MomentumSignal.created_at.desc())
        .limit(12)
    ).all()
    return [_build(session, s) for s in rows]


def last_rebalance(session: Session) -> MomentumRebalance | None:
    return session.scalar(
        select(MomentumRebalance).order_by(MomentumRebalance.rebalance_date.desc()).limit(1)
    )


def readiness(session: Session, settings: Settings) -> dict:
    """Prueft, ob die Kurshistorie fuer das Formationsfenster reicht.

    Ohne das stuende auf der Seite nur eine leere Tabelle, ohne zu sagen warum.
    """
    cfg = settings.momentum
    needed = required_history(cfg.lookback_days)
    symbols = list(
        session.scalars(
            select(Ticker.symbol).where(Ticker.active.is_(True)).order_by(Ticker.symbol)
        ).all()
    )
    counts = {
        symbol: (
            session.scalar(
                select(func.count()).select_from(Price).where(Price.symbol == symbol)
            )
            or 0
        )
        for symbol in symbols
    }
    ready = [s for s, n in counts.items() if n >= needed]
    missing = sorted(
        ((s, n) for s, n in counts.items() if n < needed), key=lambda kv: kv[1]
    )
    return {
        "needed_bars": needed,
        "ready_count": len(ready),
        "universe_size": len(symbols),
        "min_universe": cfg.min_universe,
        "sufficient": len(ready) >= cfg.min_universe,
        "missing": missing[:10],
        "max_available": max(counts.values()) if counts else 0,
    }


def current_scores(session: Session, settings: Settings) -> list[dict]:
    """Momentum-Werte aller Titel, absteigend - auch ausserhalb einer Umschichtung."""
    cfg = settings.momentum
    out: list[dict] = []
    for symbol in session.scalars(
        select(Ticker.symbol).where(Ticker.active.is_(True)).order_by(Ticker.symbol)
    ).all():
        closes = list(
            session.scalars(
                select(Price.close).where(Price.symbol == symbol).order_by(Price.date)
            ).all()
        )
        ticker = session.get(Ticker, symbol)
        out.append(
            {
                "symbol": symbol,
                "company": ticker.name if ticker else None,
                "score": momentum_score(closes, cfg.lookback_days, cfg.skip_days),
                "bars": len(closes),
            }
        )
    out.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["symbol"]))
    for index, row in enumerate(out):
        row["rank"] = index + 1 if row["score"] is not None else None
    return out


def summary(session: Session) -> dict:
    """Kennzahlen wie bei PEAD - mit Unsicherheit, ohne Punktwert bei kleinem n."""
    closed = session.scalars(
        select(MomentumSignal).where(
            MomentumSignal.status == CLOSED, MomentumSignal.return_pct.isnot(None)
        )
    ).all()
    returns = [s.return_pct for s in closed if s.return_pct is not None]
    wins = sum(1 for r in returns if r > 0)

    rate = estimate_rate(wins, len(returns))
    mean = estimate_mean(returns)

    long_returns = [s.return_pct for s in closed if s.direction == "LONG" and s.return_pct]
    short_returns = [s.return_pct for s in closed if s.direction == "SHORT" and s.return_pct]

    return {
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
        "best_return_pct": mean.maximum,
        "worst_return_pct": mean.minimum,
        "effect_distinguishable": mean.excludes_zero,
        "long_count": len(long_returns),
        "short_count": len(short_returns),
        "open_count": session.scalar(
            select(func.count()).select_from(MomentumSignal).where(MomentumSignal.status == OPEN)
        )
        or 0,
        "rebalance_count": session.scalar(
            select(func.count()).select_from(MomentumRebalance)
        )
        or 0,
    }
