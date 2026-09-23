"""Einmal geladener Marktkontext für einen Seitenaufruf.

Vorher lud jede Signalzeile die komplette Handelstagsliste ihres Titels neu.
Bei 236 Signalen über 23 Titel waren das rund 75 000 gelesene Datumszeilen
statt 7 000 - die Verlaufsseite brauchte dadurch 400 statt 100 Millisekunden,
linear wachsend mit dem Bestand.

Zwei Bedürfnisse, bewusst getrennt geladen:

* **Notierung** (letzter Kurs je Titel) - eine Aggregatabfrage über wenige
  Zeilen. Das reicht für Übersichtslisten.
* **Reihe** (alle Handelstage und Schlusskurse) - nur wo tatsächlich über
  Handelstage gerechnet wird, etwa für Restlaufzeiten oder Momentum. Beides
  pauschal zu laden machte die Titelübersicht messbar langsamer.

Der Kontext hängt an der Session, weil eine Seite mehrere Ansichten aufruft.
Ohne das läse jede davon die Kurstabelle erneut.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Price, Ticker

SESSION_KEY = "market_context"


@dataclass
class MarketContext:
    trading_dates: dict[str, list[dt.date]] = field(default_factory=dict)
    closes: dict[str, list[float]] = field(default_factory=dict)
    last_close: dict[str, float] = field(default_factory=dict)
    last_date: dict[str, dt.date] = field(default_factory=dict)
    tickers: dict[str, Ticker] = field(default_factory=dict)

    # Getrennt gefuehrt: wer nur die Notierung hat, darf nicht denken, er
    # haette auch die Reihe.
    _quoted: set[str] = field(default_factory=set)
    _seried: set[str] = field(default_factory=set)

    def dates(self, symbol: str) -> list[dt.date]:
        return self.trading_dates.get(symbol, [])

    def close_series(self, symbol: str) -> list[float]:
        return self.closes.get(symbol, [])

    def close(self, symbol: str) -> float | None:
        return self.last_close.get(symbol)

    def ticker(self, symbol: str) -> Ticker | None:
        return self.tickers.get(symbol)


def _context(session: Session) -> MarketContext:
    return session.info.setdefault(SESSION_KEY, MarketContext())


def _load_tickers(session: Session, context: MarketContext, symbols: set[str]) -> None:
    missing = symbols - set(context.tickers)
    if not missing:
        return
    for ticker in session.scalars(
        select(Ticker).where(Ticker.symbol.in_(sorted(missing)))
    ).all():
        context.tickers[ticker.symbol] = ticker


def quotes(session: Session, symbols: list[str] | set[str]) -> MarketContext:
    """Nur den letzten Kurs je Titel - eine Zeile pro Symbol."""
    context = _context(session)
    wanted = {s for s in symbols if s}
    _load_tickers(session, context, wanted)
    missing = wanted - context._quoted - context._seried
    if not missing:
        return context

    neueste = (
        select(Price.symbol, func.max(Price.date).label("tag"))
        .where(Price.symbol.in_(sorted(missing)))
        .group_by(Price.symbol)
        .subquery()
    )
    for symbol, day, close in session.execute(
        select(Price.symbol, Price.date, Price.close).join(
            neueste,
            (Price.symbol == neueste.c.symbol) & (Price.date == neueste.c.tag),
        )
    ).all():
        context.last_close[symbol] = close
        context.last_date[symbol] = day

    context._quoted |= missing
    return context


def series(session: Session, symbols: list[str] | set[str]) -> MarketContext:
    """Vollständige Kursreihe - nur wo über Handelstage gerechnet wird."""
    context = _context(session)
    wanted = {s for s in symbols if s}
    _load_tickers(session, context, wanted)
    missing = wanted - context._seried
    if not missing:
        return context

    for symbol, day, close in session.execute(
        select(Price.symbol, Price.date, Price.close)
        .where(Price.symbol.in_(sorted(missing)))
        .order_by(Price.symbol, Price.date)
    ).all():
        context.trading_dates.setdefault(symbol, []).append(day)
        context.closes.setdefault(symbol, []).append(close)
        context.last_close[symbol] = close
        context.last_date[symbol] = day

    context._seried |= missing
    context._quoted |= missing
    return context


# Rueckwaertskompatibler Name: wer die volle Reihe braucht, ruft load().
load = series
