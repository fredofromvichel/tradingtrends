"""Stammdaten ueber die Yahoo-Suche - wo Finnhub keine liefert.

Der freie Finnhub-Tarif sperrt das Unternehmensprofil fuer Titel ausserhalb
der USA. Ohne Namen bleiben die Recherche-Links wirkungslos: eine Suche nach
"AFX.DE" findet keine Nachrichten ueber Carl Zeiss Meditec.

Genommen wird die Suche, nicht ``Ticker.info``: Letzteres braucht ein
Sitzungs-Crumb, das Yahoo haeufig mit 401 verweigert. Die Suche liefert
Name, Sektor, Branche und Handelsplatz ohne diesen Umweg.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


class YahooProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class YahooProfile:
    symbol: str
    name: str | None
    sector: str | None
    industry: str | None
    exchange: str | None


def _clean(text: str | None) -> str | None:
    # Kurznamen sind auf feste Breite aufgefuellt: 'Alstom S.A.       A'
    value = " ".join((text or "").split())
    return value or None


def parse(symbol: str, quotes: list[dict]) -> YahooProfile | None:
    """Der Treffer, der genau dieses Symbol ist - nicht der erste der Liste."""
    wanted = symbol.upper()
    for q in quotes or []:
        if (q.get("symbol") or "").upper() != wanted:
            continue
        return YahooProfile(
            symbol=wanted,
            name=_clean(q.get("longname")) or _clean(q.get("shortname")),
            sector=_clean(q.get("sectorDisp") or q.get("sector")),
            industry=_clean(q.get("industryDisp") or q.get("industry")),
            exchange=_clean(q.get("exchDisp")),
        )
    return None


def fetch_profile(symbol: str) -> YahooProfile | None:  # pragma: no cover - Netzzugriff
    import yfinance as yf

    try:
        found = yf.Search(symbol, max_results=8, news_count=0, raise_errors=True)
    except Exception as exc:
        raise YahooProfileError(f"Yahoo-Suche fuer {symbol}: {exc}") from exc
    return parse(symbol, found.quotes or [])
