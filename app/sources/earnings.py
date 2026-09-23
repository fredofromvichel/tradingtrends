"""Gemeinsame Satzform der Earnings-Quellen.

Liegt bewusst neutral zwischen den Anbietern: sowohl der Finnhub-Client als
auch die Yahoo-Ausweichquelle liefern diese Form, damit die Pipeline nicht
wissen muss, woher ein Event stammt.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

FINNHUB = "finnhub"
YAHOO = "yahoo"

# Meldezeitpunkt: before market open | after market close | during market hours.
BEFORE_OPEN = "bmo"
AFTER_CLOSE = "amc"
DURING_HOURS = "dmh"


@dataclass(frozen=True)
class RawEarnings:
    """Ein Earnings-Datensatz, quellenneutral normalisiert."""

    symbol: str
    report_date: dt.date
    eps_estimate: float | None
    eps_actual: float | None
    # In Prozentpunkten (5.0 == 5 %), so liefern es beide Quellen.
    surprise_percent: float | None
    period: str | None
    # "bmo" | "amc" | "dmh" | None - entscheidet ueber den Einstiegstag.
    hour: str | None = None
    source: str = FINNHUB
