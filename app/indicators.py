"""Beschreibende Kurskennzahlen.

Wichtig zur Einordnung: nichts hier ist eine Prognose. Gleitende Durchschnitte,
52-Wochen-Spanne und Volatilitaet beschreiben, wo ein Kurs *steht* und wie stark
er schwankt - sie sagen nichts darueber, wohin er als naechstes laeuft. Die
Belege fuer eine Vorhersagekraft klassischer Chartindikatoren auf Tagesbasis
sind schwach; die Zahlen dienen hier als Kontext zur PEAD-Einordnung, nicht als
Handelssignal.

Frei von Datenbank- und Netzzugriffen (siehe tests/test_indicators.py).
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class PricePoint:
    date: dt.date
    close: float
    volume: int | None = None


@dataclass(frozen=True)
class PriceContext:
    """Momentaufnahme eines Titels, rein beschreibend."""

    last_date: dt.date | None
    last_close: float | None

    sma20: float | None
    sma50: float | None
    sma200: float | None
    # Wie sich die 200-Tage-Linie in den letzten 20 Handelstagen veraendert
    # hat - ob der langfristige Trend selbst steigt oder faellt.
    sma200_change_20d: float | None

    high_52w: float | None
    low_52w: float | None
    # 0.0 = am 52-Wochen-Tief, 1.0 = am 52-Wochen-Hoch.
    range_position: float | None

    volatility_annualised: float | None
    max_drawdown_52w: float | None

    return_1w: float | None
    return_1m: float | None
    return_3m: float | None
    return_12m: float | None

    volume_last: int | None
    volume_avg20: float | None
    volume_ratio: float | None

    bars: int

    @property
    def distance_to_high(self) -> float | None:
        """Abstand zum 52-Wochen-Hoch, negativ oder null."""
        if not self.last_close or not self.high_52w:
            return None
        return self.last_close / self.high_52w - 1

    def distance_to(self, sma: float | None) -> float | None:
        """Relativer Abstand des letzten Kurses zu einem Durchschnitt."""
        if sma is None or not self.last_close or sma <= 0:
            return None
        return (self.last_close - sma) / sma


def sma(closes: list[float], window: int) -> float | None:
    """Einfacher gleitender Durchschnitt der letzten ``window`` Schlusskurse."""
    if window < 1:
        raise ValueError("window muss mindestens 1 sein.")
    if len(closes) < window:
        return None
    return statistics.fmean(closes[-window:])


def return_over(closes: list[float], bars: int) -> float | None:
    """Rendite ueber die letzten ``bars`` Handelstage."""
    if bars < 1:
        raise ValueError("bars muss mindestens 1 sein.")
    if len(closes) < bars + 1:
        return None
    start = closes[-(bars + 1)]
    if start <= 0:
        return None
    return (closes[-1] - start) / start


def realized_volatility(closes: list[float], window: int = 60) -> float | None:
    """Annualisierte Standardabweichung der taeglichen Log-Renditen.

    Volatilitaet ist - anders als die Kursrichtung - tatsaechlich gut
    prognostizierbar und in Ruhe- wie Stressphasen klar unterschiedlich.
    Hier dient sie als Mass dafuer, wie viel Bewegung bei diesem Titel
    normal ist.
    """
    if len(closes) < 3:
        return None
    window_closes = closes[-(window + 1):] if window > 0 else closes
    log_returns = [
        math.log(b / a)
        for a, b in zip(window_closes, window_closes[1:])
        if a > 0 and b > 0
    ]
    if len(log_returns) < 2:
        return None
    return statistics.stdev(log_returns) * math.sqrt(TRADING_DAYS_PER_YEAR)


def max_drawdown(closes: list[float]) -> float | None:
    """Groesster Rueckgang vom Hoch, als negativer Bruch (-0.25 == -25 %)."""
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for close in closes:
        if close > peak:
            peak = close
        if peak > 0:
            worst = min(worst, (close - peak) / peak)
    return worst


def range_position(current: float, low: float, high: float) -> float | None:
    """Position innerhalb einer Spanne: 0.0 am Tief, 1.0 am Hoch."""
    if high <= low:
        return None
    return max(0.0, min(1.0, (current - low) / (high - low)))


def _change(now: float | None, before: float | None) -> float | None:
    if now is None or before is None or before <= 0:
        return None
    return now / before - 1


def compute_context(points: list[PricePoint]) -> PriceContext:
    """Verdichtet eine aufsteigend sortierte Kursreihe zu Kennzahlen."""
    if not points:
        return PriceContext(
            None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, 0,
        )

    closes = [p.close for p in points]
    last = points[-1]

    window_52w = points[-TRADING_DAYS_PER_YEAR:]
    closes_52w = [p.close for p in window_52w]
    high = max(closes_52w)
    low = min(closes_52w)

    volumes = [p.volume for p in points[-20:] if p.volume is not None]
    volume_avg20 = statistics.fmean(volumes) if len(volumes) >= 5 else None
    volume_ratio = (
        (last.volume / volume_avg20)
        if (last.volume is not None and volume_avg20 and volume_avg20 > 0)
        else None
    )

    return PriceContext(
        last_date=last.date,
        last_close=last.close,
        sma20=sma(closes, 20),
        sma50=sma(closes, 50),
        sma200=sma(closes, 200),
        sma200_change_20d=_change(sma(closes, 200), sma(closes[:-20], 200)),
        high_52w=high,
        low_52w=low,
        range_position=range_position(last.close, low, high),
        volatility_annualised=realized_volatility(closes),
        max_drawdown_52w=max_drawdown(closes_52w),
        return_1w=return_over(closes, 5),
        return_1m=return_over(closes, 21),
        return_3m=return_over(closes, 63),
        return_12m=return_over(closes, TRADING_DAYS_PER_YEAR),
        volume_last=last.volume,
        volume_avg20=volume_avg20,
        volume_ratio=volume_ratio,
        bars=len(points),
    )
