"""Earnings über yfinance - Ausweichquelle, wo Finnhubs Tarif nicht greift.

Yahoo liefert in einem Abruf Meldedatum, EPS-Schätzung, tatsächliches EPS und
Surprise, oft über 25 Quartale. Damit deckt diese Quelle die Titel ab, für die
Finnhubs Free-Tier mit 403 antwortet - typischerweise alles ausserhalb der USA.

Zwei bewusste Einschränkungen:

* **Der Meldezeitpunkt wird nur für US-Titel abgeleitet.** Yahoo gibt alle
  Zeitstempel in New Yorker Zeit aus. Bei AAPL um 16:00 ET ist "nach
  Börsenschluss" eindeutig; bei SAP.DE um "20:00 ET" ist der Wert eine
  Umrechnung, aus der sich die Frankfurter Meldezeit nicht rekonstruieren
  lässt. Dort bleibt der Zeitpunkt unbekannt - die Signal-Logik steigt dann
  am Folgetag ein, also auf der sicheren Seite.
* **Das Meldedatum wird in New Yorker Zeit genommen.** Fällt die wahre lokale
  Meldung dadurch einen Tag später, greift genau derselbe Schutz: bei
  unbekanntem Zeitpunkt beginnt die Position ohnehin erst am nächsten
  Handelstag, also nach der Nachricht. Ein zu früher Einstieg ist damit in
  beiden Lesarten ausgeschlossen.
"""

from __future__ import annotations

import datetime as dt
import logging

import yfinance as yf

from app.sources.earnings import (
    AFTER_CLOSE,
    BEFORE_OPEN,
    DURING_HOURS,
    YAHOO,
    RawEarnings,
)

log = logging.getLogger(__name__)

# US-Handelszeiten in New Yorker Zeit.
MARKET_OPEN = dt.time(9, 30)
MARKET_CLOSE = dt.time(16, 0)


class YahooEarningsError(RuntimeError):
    """Abruf fehlgeschlagen - die Pipeline behandelt das wie jeden Quellenfehler."""


def is_us_symbol(symbol: str) -> bool:
    """Yahoo haengt an nicht-amerikanische Symbole ein Boersensuffix an."""
    return "." not in symbol


def derive_hour(symbol: str, stamp: dt.datetime) -> str | None:
    """Meldezeitpunkt aus dem Zeitstempel - nur wo er belastbar ist."""
    if not is_us_symbol(symbol):
        return None
    clock = stamp.time()
    if clock < MARKET_OPEN:
        return BEFORE_OPEN
    if clock >= MARKET_CLOSE:
        return AFTER_CLOSE
    return DURING_HOURS


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN aussortieren


def fetch_earnings(symbol: str, limit: int = 24) -> list[RawEarnings]:
    """Gemeldete Quartale, neueste zuerst.

    Noch nicht gemeldete Termine (ohne tatsächliches EPS) bleiben draussen -
    die Signal-Logik braucht ein Ergebnis, keine Ankündigung.
    """
    try:
        frame = yf.Ticker(symbol).get_earnings_dates(limit=limit)
    except Exception as exc:  # yfinance wirft je nach Version Unterschiedliches
        raise YahooEarningsError(f"yfinance-Abruf fuer {symbol} fehlgeschlagen: {exc}") from exc

    if frame is None or frame.empty:
        return []

    out: list[RawEarnings] = []
    for stamp, row in frame.iterrows():
        actual = _as_float(row.get("Reported EPS"))
        if actual is None:
            continue   # noch nicht gemeldet
        try:
            report_date = stamp.date()
        except AttributeError:
            continue
        out.append(
            RawEarnings(
                symbol=symbol,
                report_date=report_date,
                eps_estimate=_as_float(row.get("EPS Estimate")),
                eps_actual=actual,
                surprise_percent=_as_float(row.get("Surprise(%)")),
                period=report_date.isoformat(),
                hour=derive_hour(symbol, stamp),
                source=YAHOO,
            )
        )
    out.sort(key=lambda r: r.report_date, reverse=True)
    return out


def fetch_next_earnings_date(symbol: str, today: dt.date | None = None) -> dt.date | None:
    """Nächster erwarteter Meldetermin, falls Yahoo einen kennt.

    Yahoos Kalender enthält auch bereits verstrichene Termine. Ein solcher
    wäre nicht nur falsch angezeigt: die Pipeline holt den Ausblick erneut,
    sobald der gemerkte Termin in der Vergangenheit liegt - ein vergangener
    Wert löste also bei jedem Lauf einen überflüssigen Abruf aus.
    """
    today = today or dt.date.today()
    try:
        calendar = yf.Ticker(symbol).calendar
    except Exception as exc:
        raise YahooEarningsError(f"yfinance-Kalender fuer {symbol}: {exc}") from exc

    if not isinstance(calendar, dict):
        return None
    dates = calendar.get("Earnings Date") or []
    if isinstance(dates, dt.date):
        dates = [dates]
    upcoming = sorted(d for d in dates if isinstance(d, dt.date) and d >= today)
    return upcoming[0] if upcoming else None
