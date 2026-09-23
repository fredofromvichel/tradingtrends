"""Rueckwirkende Momentum-Signale: was die Regel im letzten Jahr gesagt haette.

Fuer jeden Monatsanfang wird das heutige Universum so rangiert, wie es der
Live-Lauf an diesem Tag getan haette - mit den Kursen bis zu diesem Tag, nicht
danach. Einstieg zum Schlusskurs des Umschichtungstags, Ausstieg nach der
Haltedauer, Vergleich mit dem Schnitt der uebrigen Titel.

Drei Gruende, warum das Ergebnis besser aussieht, als es damals gewesen waere:

* Das Universum ist das heutige. Wer seine Titel mit Kenntnis ihrer
  Vergangenheit ausgewaehlt hat, hat Verlierer eher weggelassen.
* Die Kurse sind nachtraeglich um Splits und Dividenden bereinigt.
* Nichts davon ist gehandelt worden - ohne Gebuehren, ohne Spread.

Deshalb wird es nie gespeichert und nie mit den Live-Positionen verrechnet,
sondern bei Bedarf aus den gespeicherten Kursen neu berechnet. Das haelt es
auch aktuell, wenn Titel hinzukommen oder wegfallen.

Der Kern ist frei von Datenbankzugriffen (siehe tests/test_retro.py).
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass, field

from app.momentum import LONG, momentum_score, rank_universe
from app.stats import MIN_SAMPLE, estimate_mean, estimate_rate

# So viele Monate reicht die Rueckrechnung - so weit wie das Kursdiagramm.
MONTHS = 12
# Unterhalb dieser Zahl an Vergleichswerten bleibt der Vergleich leer, wie live.
MIN_BENCHMARK = 3

Series = dict[str, tuple[list[dt.date], list[float]]]


@dataclass(frozen=True)
class RetroPosition:
    symbol: str
    direction: str              # LONG | SHORT
    rank: int
    universe_size: int
    group_size: int
    score: float
    rebalance_date: dt.date
    entry_date: dt.date
    entry_price: float
    exit_date: dt.date | None   # None: Haltedauer laeuft noch
    exit_price: float | None
    return_pct: float | None    # nur bei abgeschlossener Haltedauer
    unrealized_pct: float | None
    benchmark_return_pct: float | None

    @property
    def closed(self) -> bool:
        return self.exit_date is not None


@dataclass(frozen=True)
class RetroMonth:
    rebalance_date: dt.date
    universe_size: int
    group_size: int
    positions: tuple[RetroPosition, ...] = ()
    # Warum nicht rangiert wurde - meist zu kurze Kurshistorie.
    reason: str = ""


@dataclass
class RetroHistory:
    months: list[RetroMonth] = field(default_factory=list)

    @property
    def positions(self) -> list[RetroPosition]:
        return [p for m in self.months for p in m.positions]

    @property
    def covered_from(self) -> dt.date | None:
        """Erster Monat, fuer den eine Rangfolge moeglich war."""
        ranked = [m.rebalance_date for m in self.months if m.positions]
        return min(ranked) if ranked else None

    def for_symbol(self, symbol: str) -> list[RetroPosition]:
        return [p for p in self.positions if p.symbol == symbol]


def rebalance_dates(calendar: list[dt.date], start: dt.date, end: dt.date) -> list[dt.date]:
    """Erster Handelstag jedes Monats in [start, end).

    So schichtet auch der Live-Lauf um: beim ersten Lauf eines neuen Monats.
    """
    out: list[dt.date] = []
    seen: set[tuple[int, int]] = set()
    for day in calendar:
        if day < start or day >= end:
            continue
        key = (day.year, day.month)
        if key not in seen:
            seen.add(key)
            out.append(day)
    return out


def _close_on_or_before(dates: list[dt.date], closes: list[float], day: dt.date) -> float | None:
    index = bisect.bisect_right(dates, day) - 1
    return closes[index] if index >= 0 else None


def _benchmark(series: Series, exclude: str, start: dt.date, end: dt.date) -> float | None:
    """Gleichgewichteter Schnitt der uebrigen Titel - wie live beim Schliessen."""
    renditen: list[float] = []
    for symbol, (dates, closes) in series.items():
        if symbol == exclude:
            continue
        anfang = _close_on_or_before(dates, closes, start)
        ende = _close_on_or_before(dates, closes, end)
        # Nur wer zu Beginn schon handelte, zaehlt - sonst verglichen wir mit
        # einem Kurs, den es an diesem Tag noch nicht gab.
        if anfang is None or ende is None or anfang <= 0 or dates[0] > start:
            continue
        renditen.append((ende - anfang) / anfang)
    if len(renditen) < MIN_BENCHMARK:
        return None
    return sum(renditen) / len(renditen)


def momentum_history(
    series: Series,
    *,
    lookback_days: int,
    skip_days: int,
    holding_period_days: int,
    group_fraction: float,
    min_universe: int,
    start: dt.date,
    end: dt.date,
) -> RetroHistory:
    """Rangfolgen und Positionen fuer jeden Monatsanfang in [start, end).

    ``series`` bildet Symbol -> (Handelstage, Schlusskurse), aufsteigend.
    """
    calendar = sorted({d for dates, _ in series.values() for d in dates})
    history = RetroHistory()

    for day in rebalance_dates(calendar, start, end):
        scores: dict[str, float | None] = {}
        for symbol, (dates, closes) in series.items():
            # Nur Kurse bis einschliesslich des Umschichtungstags - was danach
            # kam, wusste der Lauf an diesem Tag nicht.
            cut = bisect.bisect_right(dates, day)
            scores[symbol] = momentum_score(closes[:cut], lookback_days, skip_days)

        basket = rank_universe(scores, group_fraction, min_universe)
        if basket.is_empty:
            history.months.append(
                RetroMonth(day, basket.universe_size, basket.group_size, reason=basket.reason)
            )
            continue

        positions: list[RetroPosition] = []
        for ranked in basket.longs + basket.shorts:
            dates, closes = series[ranked.symbol]
            entry_index = bisect.bisect_left(dates, day)
            if entry_index >= len(dates):
                continue
            entry_date, entry_price = dates[entry_index], closes[entry_index]
            exit_index = entry_index + holding_period_days
            sign = 1 if ranked.direction == LONG else -1

            if exit_index < len(dates):
                exit_date, exit_price = dates[exit_index], closes[exit_index]
                ret = sign * (exit_price - entry_price) / entry_price
                unrealized = None
                bench_end = exit_date
            else:
                exit_date = exit_price = ret = None
                unrealized = sign * (closes[-1] - entry_price) / entry_price
                bench_end = dates[-1]

            bench = _benchmark(series, ranked.symbol, entry_date, bench_end)
            positions.append(
                RetroPosition(
                    symbol=ranked.symbol,
                    direction=ranked.direction,
                    rank=ranked.rank,
                    universe_size=basket.universe_size,
                    group_size=basket.group_size,
                    score=ranked.score,
                    rebalance_date=day,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    exit_date=exit_date,
                    exit_price=exit_price,
                    return_pct=ret,
                    unrealized_pct=unrealized,
                    # Wie live: was die uebrigen Titel gemacht haben, ohne
                    # Vorzeichenwechsel bei SHORT - "haette ich stattdessen
                    # einfach die anderen gehalten".
                    benchmark_return_pct=bench,
                )
            )
        history.months.append(
            RetroMonth(day, basket.universe_size, basket.group_size, tuple(positions))
        )
    return history


def summarize(positions: list[RetroPosition]) -> dict:
    """Kennzahlen im selben Format wie views.summary - mit origin 'retro'."""
    closed = [p for p in positions if p.return_pct is not None]
    returns = [p.return_pct for p in closed]
    wins = sum(1 for r in returns if r > 0)
    beats = [
        p.return_pct - p.benchmark_return_pct
        for p in closed if p.benchmark_return_pct is not None
    ]
    rate = estimate_rate(wins, len(returns))
    mean = estimate_mean(returns)
    return {
        "origin": "retro",
        "closed_count": len(returns),
        "win_count": wins,
        "min_sample": MIN_SAMPLE,
        "reliable": rate.reliable,
        "win_rate": rate.point,
        "win_rate_low": rate.low,
        "win_rate_high": rate.high,
        "avg_return_pct": mean.mean,
        "avg_return_low": mean.low,
        "avg_return_high": mean.high,
        "beat_count": sum(1 for b in beats if b > 0),
        "beat_base": len(beats),
        "open_count": sum(1 for p in positions if p.return_pct is None),
    }
