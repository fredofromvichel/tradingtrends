"""Signal-Logik: Surprise, SUE, Schwellenwert-Entscheidung, Rendite.

Bewusst frei von Datenbank- und Netzzugriffen, damit die Kernlogik ohne
Infrastruktur getestet werden kann (siehe tests/test_signals.py).
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass

BUY = "BUY"
SELL = "SELL"
OPEN = "OPEN"
CLOSED = "CLOSED"

# Unterhalb dieser Groesse ist die EPS-Schaetzung praktisch null; eine
# prozentuale Surprise darauf ist nicht aussagekraeftig.
_MIN_ABS_ESTIMATE = 1e-4


@dataclass(frozen=True)
class Decision:
    signal_type: str | None  # BUY | SELL | None
    sue: float | None
    surprise_pct: float | None
    basis: str  # "sue" | "surprise_pct" | "none"
    reason: str


def compute_surprise_pct(
    eps_actual: float | None,
    eps_estimate: float | None,
    finnhub_surprise_percent: float | None = None,
) -> float | None:
    """Relative Gewinnueberraschung als Bruch (0.05 == +5 %).

    Primaer aus actual/estimate berechnet. Ist die Schaetzung null oder
    verschwindend klein, wird auf Finnhubs eigenen Wert zurueckgegriffen
    (dort in Prozentpunkten gefuehrt).
    """
    if (
        eps_actual is not None
        and eps_estimate is not None
        and abs(eps_estimate) >= _MIN_ABS_ESTIMATE
    ):
        return (eps_actual - eps_estimate) / abs(eps_estimate)
    if finnhub_surprise_percent is not None:
        return finnhub_surprise_percent / 100.0
    return None


def sue_ingredients(
    surprise_pct: float | None,
    history: list[float],
    min_history: int = 4,
) -> tuple[float | None, float | None, int]:
    """SUE samt seiner Zutaten: (sue, verwendete Streuung, Anzahl Quartale).

    Die Streuung wird mitgegeben, damit der Rechenweg spaeter nachvollziehbar
    bleibt - "42 % geteilt durch die uebliche Schwankung von 39 %" ist die
    Aussage, nicht die nackte Zahl 1.08.
    """
    clean = [h for h in history if h is not None]
    if surprise_pct is None or len(clean) < min_history:
        return None, None, len(clean)
    try:
        std = statistics.stdev(clean)
    except statistics.StatisticsError:
        return None, None, len(clean)
    if std <= 0:
        return None, std, len(clean)
    return surprise_pct / std, std, len(clean)


def compute_sue(
    surprise_pct: float | None,
    history: list[float],
    min_history: int = 4,
) -> float | None:
    """Standardisierte Ueberraschung: surprise_pct / stdev(historische Surprises).

    ``history`` enthaelt die Surprises frueherer Quartale (ohne das aktuelle
    Event), ebenfalls als Bruch. Liegen weniger als ``min_history`` Werte vor
    oder ist die Streuung null, wird None zurueckgegeben - dann greift der
    surprise_pct-Fallback.
    """
    if surprise_pct is None:
        return None
    clean = [h for h in history if h is not None]
    if len(clean) < min_history:
        return None
    try:
        std = statistics.stdev(clean)
    except statistics.StatisticsError:
        return None
    if std <= 0:
        return None
    return surprise_pct / std


def decide(
    surprise_pct: float | None,
    sue: float | None,
    *,
    sue_threshold_buy: float,
    sue_threshold_sell: float,
    surprise_pct_fallback_buy: float,
    surprise_pct_fallback_sell: float,
) -> Decision:
    """Wendet die Schwellenwerte an. SUE hat Vorrang, sonst der Rohwert."""
    if surprise_pct is None and sue is None:
        return Decision(None, sue, surprise_pct, "none", "Keine Surprise-Daten vorhanden.")

    if sue is not None:
        if sue > sue_threshold_buy:
            return Decision(BUY, sue, surprise_pct, "sue", f"SUE {sue:.2f} > {sue_threshold_buy}")
        if sue < sue_threshold_sell:
            return Decision(
                SELL, sue, surprise_pct, "sue", f"SUE {sue:.2f} < {sue_threshold_sell}"
            )
        return Decision(
            None,
            sue,
            surprise_pct,
            "sue",
            f"SUE {sue:.2f} liegt zwischen {sue_threshold_sell} und {sue_threshold_buy}",
        )

    if surprise_pct > surprise_pct_fallback_buy:
        return Decision(
            BUY,
            sue,
            surprise_pct,
            "surprise_pct",
            f"Surprise {surprise_pct:.2%} > {surprise_pct_fallback_buy:.2%} (ohne Historie)",
        )
    if surprise_pct < surprise_pct_fallback_sell:
        return Decision(
            SELL,
            sue,
            surprise_pct,
            "surprise_pct",
            f"Surprise {surprise_pct:.2%} < {surprise_pct_fallback_sell:.2%} (ohne Historie)",
        )
    return Decision(
        None,
        sue,
        surprise_pct,
        "surprise_pct",
        f"Surprise {surprise_pct:.2%} innerhalb der Schwellen (ohne Historie)",
    )


def compute_return_pct(signal_type: str, entry_price: float, exit_price: float) -> float:
    """Rendite als Bruch. SELL wird als Short-Position gerechnet."""
    if entry_price <= 0:
        raise ValueError("entry_price muss positiv sein.")
    raw = (exit_price - entry_price) / entry_price
    return raw if signal_type == BUY else -raw


def trading_days_elapsed(trading_dates: list[dt.date], trigger_date: dt.date) -> int:
    """Anzahl Handelstage nach dem Trigger-Datum.

    ``trading_dates`` ist die aufsteigend sortierte Liste vorhandener
    Kurstage des Titels. Der Einstiegstag selbst zaehlt nicht mit.
    """
    return sum(1 for d in trading_dates if d > trigger_date)


# Finnhub kennzeichnet den Meldezeitpunkt: "bmo" = before market open,
# "amc" = after market close, "dmh" = during market hours.
BEFORE_MARKET_OPEN = "bmo"


def find_entry_day(
    trading_dates: list[dt.date],
    trigger_date: dt.date,
    report_hour: str | None = None,
) -> dt.date | None:
    """Erster handelbarer Schlusskurs nach der Meldung.

    Nur bei einer Meldung VOR Handelsbeginn ("bmo") ist der Schlusskurs des
    Meldetages selbst erreichbar. Wird nach Handelsschluss oder waehrend des
    Handels gemeldet - und ebenso, wenn der Zeitpunkt unbekannt ist -, liegt
    der Schlusskurs des Meldetages ganz oder teilweise vor der Nachricht.
    Ihn als Einstieg zu nehmen wuerde den Kurssprung ueber Nacht
    vorwegnehmen (Lookahead-Bias), deshalb wird dann der naechste Handelstag
    genommen.
    """
    after_close = (report_hour or "").strip().lower() != BEFORE_MARKET_OPEN
    for day in trading_dates:
        if day > trigger_date or (day == trigger_date and not after_close):
            return day
    return None


def find_exit_day(
    trading_dates: list[dt.date], entry_date: dt.date, holding_period_days: int
) -> dt.date | None:
    """Handelstag, an dem die Position faellig wird.

    Gezaehlt werden Handelstage *nach* dem Einstieg; erreicht der Zaehler
    ``holding_period_days``, ist dieser Tag der Ausstiegstag. Fehlen noch
    Kurstage, bleibt die Position offen (None).
    """
    if holding_period_days < 1:
        raise ValueError("holding_period_days muss mindestens 1 sein.")
    count = 0
    for day in trading_dates:
        if day <= entry_date:
            continue
        count += 1
        if count >= holding_period_days:
            return day
    return None
