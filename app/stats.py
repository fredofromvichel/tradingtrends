"""Statistische Kennzahlen mit ausgewiesener Unsicherheit.

Der Zweck dieses Moduls ist, aus einer kleinen Stichprobe *keine* Scheingenauigkeit
zu erzeugen. Jede Kennzahl kommt mit einem Konfidenzintervall, und unterhalb von
``MIN_SAMPLE`` geschlossenen Positionen wird der Punktschaetzer bewusst
zurueckgehalten - bei fuenf Beobachtungen ist eine "Trefferquote von 60 %" eine
Zahl ohne Inhalt.

Frei von Datenbank- und Netzzugriffen (siehe tests/test_stats.py).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

# Ab dieser Anzahl geschlossener Positionen werden Punktschaetzer angezeigt.
# Der Wert ist eine Konvention, keine magische Grenze: auch bei 20 Positionen
# ist das Konfidenzintervall noch breit - es ist nur nicht mehr voellig
# nichtssagend.
MIN_SAMPLE = 20

Z_95 = 1.959963985

# Zweiseitige kritische t-Werte fuer 95 %, nach Freiheitsgraden.
# Ab df > 30 reicht die Normalapproximation.
_T_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def t_critical_95(df: int) -> float:
    if df < 1:
        raise ValueError("df muss mindestens 1 sein.")
    return _T_95.get(df, Z_95)


@dataclass(frozen=True)
class RateEstimate:
    """Anteilsschaetzung mit Wilson-Konfidenzintervall."""

    successes: int
    n: int
    point: float | None       # None, solange n < MIN_SAMPLE
    low: float
    high: float
    reliable: bool

    @property
    def width(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class MeanEstimate:
    """Mittelwert mit t-Konfidenzintervall plus beschreibende Kennzahlen."""

    n: int
    mean: float | None        # None, solange n < MIN_SAMPLE
    low: float | None
    high: float | None
    median: float | None
    stdev: float | None
    minimum: float | None
    maximum: float | None
    reliable: bool
    # True, wenn das Intervall die Null nicht enthaelt - erst dann ist der
    # Mittelwert ueberhaupt von "kein Effekt" unterscheidbar.
    excludes_zero: bool


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """95 %-Konfidenzintervall fuer einen Anteil nach Wilson.

    Gegenueber der Normalapproximation auch bei kleinem n und Anteilen nahe
    0 oder 1 brauchbar - genau der Bereich, in dem dieser POC sich bewegt.
    """
    if n <= 0:
        return (0.0, 1.0)
    if successes < 0 or successes > n:
        raise ValueError("successes muss zwischen 0 und n liegen.")

    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def estimate_rate(successes: int, n: int, min_sample: int = MIN_SAMPLE) -> RateEstimate:
    low, high = wilson_interval(successes, n)
    reliable = n >= min_sample
    return RateEstimate(
        successes=successes,
        n=n,
        point=(successes / n) if (reliable and n > 0) else None,
        low=low,
        high=high,
        reliable=reliable,
    )


def estimate_mean(values: list[float], min_sample: int = MIN_SAMPLE) -> MeanEstimate:
    """Mittelwert mit t-Intervall. Bei n < 2 gibt es keine Streuung zu schaetzen."""
    clean = [v for v in values if v is not None]
    n = len(clean)
    if n == 0:
        return MeanEstimate(0, None, None, None, None, None, None, None, False, False)

    mean = statistics.fmean(clean)
    median = statistics.median(clean)
    reliable = n >= min_sample

    if n < 2:
        return MeanEstimate(
            n=n, mean=mean if reliable else None, low=None, high=None,
            median=median, stdev=None, minimum=min(clean), maximum=max(clean),
            reliable=reliable, excludes_zero=False,
        )

    stdev = statistics.stdev(clean)
    margin = t_critical_95(n - 1) * stdev / math.sqrt(n)
    low, high = mean - margin, mean + margin

    return MeanEstimate(
        n=n,
        mean=mean if reliable else None,
        low=low,
        high=high,
        median=median,
        stdev=stdev,
        minimum=min(clean),
        maximum=max(clean),
        reliable=reliable,
        excludes_zero=(low > 0 or high < 0),
    )
