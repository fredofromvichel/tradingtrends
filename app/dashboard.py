"""Daten für die Übersichtsseite.

Leitgedanke: der häufige Fall soll ohne Navigation auskommen. Wer morgens
schaut, will wissen ob etwas ansteht, was gerade läuft und was demnächst
passiert - nicht erst durch vier Seiten klicken.

Deshalb bündelt diese Seite beide Signalquellen, ordnet das Dringende nach
oben und nennt die nächsten Termine. Die Detailseiten bleiben für alles, was
darüber hinausgeht.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import market_context, momentum_view, views, watchlist
from app.config import Settings
from app.models import Ticker

# Fenster für "demnächst" - weit genug, um Vorlauf zu geben, eng genug,
# dass die Liste nicht zum Kalender wird.
UPCOMING_DAYS = 21
# Ab wann eine Position als "läuft bald aus" gilt.
ENDING_SOON_DAYS = 5


@dataclass
class Upcoming:
    symbol: str
    company: str | None
    date: str
    days_away: int


@dataclass
class Dashboard:
    pead_open: list
    pead_summary: dict
    momentum_open: list
    momentum_summary: dict
    upcoming: list[Upcoming]
    ending_soon: list
    ticker_count: int
    upcoming_days: int
    ending_soon_days: int


def build(session: Session, settings: Settings) -> Dashboard:
    pead_open = views.open_signals(session)
    momentum_open = momentum_view.open_positions(session)

    today = dt.date.today()
    horizon = today + dt.timedelta(days=UPCOMING_DAYS)
    tickers = session.scalars(
        select(Ticker)
        .where(
            Ticker.active.is_(True),
            Ticker.next_earnings_date.isnot(None),
            Ticker.next_earnings_date >= today,
            Ticker.next_earnings_date <= horizon,
        )
        .order_by(Ticker.next_earnings_date)
    ).all()
    market_context.quotes(session, [t.symbol for t in tickers])

    upcoming = [
        Upcoming(
            symbol=t.symbol,
            company=t.name,
            date=t.next_earnings_date.isoformat(),
            days_away=(t.next_earnings_date - today).days,
        )
        for t in tickers
    ]

    # Positionen, bei denen bald etwas passiert - nach Dringlichkeit sortiert.
    ending_soon = sorted(
        (
            s
            for s in list(pead_open) + list(momentum_open)
            if s.days_remaining is not None and s.days_remaining <= ENDING_SOON_DAYS
        ),
        key=lambda s: s.days_remaining,
    )

    return Dashboard(
        pead_open=pead_open,
        pead_summary=views.summary(session),
        momentum_open=momentum_open,
        momentum_summary=momentum_view.summary(session),
        upcoming=upcoming,
        ending_soon=ending_soon,
        ticker_count=len(watchlist.active_symbols(session)),
        upcoming_days=UPCOMING_DAYS,
        ending_soon_days=ENDING_SOON_DAYS,
    )
