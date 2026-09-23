"""Symbolpruefung vor dem Aufnehmen eines Titels.

Deutsche Portale fuehren eigene Kuerzel, und dieselbe Buchstabenfolge steht
an verschiedenen Boersen fuer verschiedene Firmen: ``AOMD`` ist an der NYSE
ein Hypotheken-REIT, an Xetra und in Frankfurt der Zugbauer Alstom. Wer ein
Kuerzel ungeprueft uebernimmt, bekommt klaglos Signale fuer die falsche Firma.

Deshalb loest die Pruefung eine Eingabe - Kuerzel, Yahoo-Symbol oder
Firmenname - in Unternehmen mit ihren Handelsplaetzen auf, zeigt den letzten
Kurs als Plausibilitaetsprobe und empfiehlt je Unternehmen ein Listing:

1. US-Boerse - nur dort liefert Finnhub im freien Tarif Earnings.
2. Xetra - liquidester deutscher Handelsplatz, Kurse in Euro.
3. Frankfurt, dann Heimatboersen, dann Regionalboersen, OTC zuletzt.

Die Yahoo-Zugriffe (``search_quotes``, ``probe_prices``) sind eigene
Funktionen, damit Tests sie ersetzen koennen.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MAX_QUERIES = 10
MAX_COMPANIES = 4
MAX_LISTINGS_PER_COMPANY = 4
MAX_NAME_SEARCHES = 3
# Nur Aktien: ETFs melden keine Earnings, und die Suche nach einem Kuerzel
# wie NVD bringt sonst ein Dutzend gehebelter Produkte auf den Basiswert.
ALLOWED_TYPES = {"EQUITY"}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,15}$")

US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "NAS", "NYS"}
OTC_EXCHANGES = {"PNK", "OQB", "OQX", "OBB", "OTC"}
GERMAN_REGIONAL = {"MUN", "HAN", "HAM", "STU", "DUS", "BER"}
HOME_EXCHANGES = {
    "PAR", "LSE", "AMS", "BRU", "MIL", "MCE", "EBS", "VTX", "CPH", "STO",
    "HEL", "OSL", "LIS", "VIE", "HKG", "TYO", "TOR", "ASX", "KSC", "TAI",
}
CURRENCY = {
    **{code: "USD" for code in US_EXCHANGES | OTC_EXCHANGES},
    **{code: "EUR" for code in {"GER", "FRA", "PAR", "AMS", "BRU", "MIL", "MCE",
                                "HEL", "LIS", "VIE"} | GERMAN_REGIONAL},
    "LSE": "GBp", "EBS": "CHF", "VTX": "CHF", "HKG": "HKD", "TYO": "JPY",
    "TOR": "CAD", "ASX": "AUD", "CPH": "DKK", "STO": "SEK", "OSL": "NOK",
}


@dataclass(frozen=True)
class Quote:
    """Ein Treffer der Yahoo-Suche."""
    symbol: str
    name: str
    exchange_code: str
    exchange: str
    quote_type: str = "EQUITY"


@dataclass
class Listing:
    symbol: str
    exchange: str
    exchange_code: str
    currency: str | None = None
    last_close: float | None = None
    last_date: dt.date | None = None
    # 'active' | 'inactive' | None - wird von der Oberflaeche gesetzt.
    known: str | None = None

    @property
    def us(self) -> bool:
        return self.exchange_code in US_EXCHANGES

    @property
    def otc(self) -> bool:
        return self.exchange_code in OTC_EXCHANGES


@dataclass
class Company:
    name: str
    listings: list[Listing] = field(default_factory=list)

    @property
    def recommended(self) -> Listing | None:
        return self.listings[0] if self.listings else None

    @property
    def reason(self) -> str:
        best = self.recommended
        if best is None:
            return ""
        if best.us:
            return "US-Listing: Earnings kommen von Finnhub."
        if best.exchange_code == "GER":
            return "Xetra: liquidester deutscher Handelsplatz. Earnings über Yahoo."
        if best.otc:
            return ("Nur außerbörslich (OTC) gefunden. Kurse sind dünn gehandelt, "
                    "Earnings unsicher.")
        return "Earnings über Yahoo, soweit dort geführt."


@dataclass
class Lookup:
    query: str
    companies: list[Company] = field(default_factory=list)
    error: str | None = None
    # Vorauswahl im Formular; setzt die Oberflaeche, weil sie weiss, was
    # schon beobachtet wird (siehe watchlist_view.mark_known).
    choice: str | None = None

    @property
    def ambiguous(self) -> bool:
        """Mehrere Firmen hinter einer Eingabe - dann nichts vorauswaehlen."""
        return len(self.companies) > 1

    @property
    def preselected(self) -> str | None:
        if len(self.companies) == 1 and self.companies[0].recommended:
            return self.companies[0].recommended.symbol
        return None


# -- Eingabe ------------------------------------------------------------------


def split_queries(raw: str) -> list[str]:
    """Mehrere Eingaben, getrennt durch Komma, Semikolon oder Zeilenumbruch.

    Leerzeichen trennen bewusst nicht: 'Carl Zeiss Meditec' ist ein Name.
    """
    parts = re.split(r"[,;\n]+", raw or "")
    seen: dict[str, None] = {}
    for part in parts:
        text = " ".join(part.split())
        if text and text.upper() not in {k.upper() for k in seen}:
            seen[text] = None
    return list(seen)[:MAX_QUERIES]


def normalize_symbol(raw: str) -> str | None:
    """Ein Symbol in Yahoo-Schreibweise, oder None bei unzulaessigen Zeichen."""
    text = (raw or "").strip().upper()
    return text if SYMBOL_PATTERN.match(text) else None


# Rechtsformen am Namensende. Yahoo schreibt dieselbe Firma je Handelsplatz
# unterschiedlich ('BYD Company Limited', 'BYD Co Ltd').
LEGAL_FORMS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "plc", "ag", "aktiengesellschaft", "sa", "se", "nv", "bv",
    "spa", "kgaa", "gmbh", "ab", "asa", "oyj", "sas", "societe", "anonyme",
    "holding", "holdings", "group",
}


def _words(text: str) -> list[str]:
    # 'société' -> 'societe': sonst zerfiele das Wort am Akzent.
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).split()


def _company_key(name: str) -> str:
    tokens = _words(name)
    while len(tokens) > 1 and tokens[-1] in LEGAL_FORMS:
        tokens.pop()
    return "".join(tokens)


def _clean_name(name: str | None) -> str:
    # Yahoos Kurznamen sind auf feste Breite aufgefuellt: 'Alstom S.A.      A'
    return " ".join((name or "").split())


def _rank(listing: Listing) -> int:
    code = listing.exchange_code
    if code in US_EXCHANGES:
        return 0
    if code == "GER":
        return 1
    if code == "FRA":
        return 2
    if code in HOME_EXCHANGES:
        return 3
    if code in GERMAN_REGIONAL:
        return 5
    if code in OTC_EXCHANGES:
        return 6
    return 4


def _relevance(company: Company, query: str, symbol: str | None) -> int | None:
    """0 = Symbol passt, 1 = Name passt, None = Beifang der unscharfen Suche."""
    if symbol and any(
        l.symbol == symbol or l.symbol.split(".")[0] == symbol for l in company.listings
    ):
        return 0
    words = set(_words(company.name))
    wanted = _words(query)
    if wanted and all(w in words for w in wanted):
        return 1
    return None


# -- Aufloesung ---------------------------------------------------------------


def resolve(
    query: str,
    search: Callable[[str], list[Quote]],
    prices: Callable[[list[str]], dict[str, tuple[dt.date, float]]],
) -> Lookup:
    """Loest eine Eingabe in Unternehmen und empfohlene Listings auf."""
    result = Lookup(query=query)
    symbol = normalize_symbol(query)
    # Kuerzel deutscher Portale liegen bei Yahoo meist unter .DE oder .F -
    # und die unscharfe Suche nach 'AIR' findet Fluglinien, aber nicht Airbus.
    probes = [query]
    if symbol and "." not in symbol:
        probes += [f"{symbol}.DE", f"{symbol}.F"]
    try:
        quotes = []
        for probe in probes:
            quotes += [q for q in search(probe) if q.quote_type in ALLOWED_TYPES]
    except Exception as exc:
        log.warning("Yahoo-Suche fuer '%s' fehlgeschlagen: %s", query, exc)
        result.error = "Yahoo ist gerade nicht erreichbar. Bitte später erneut prüfen."
        return result

    # Die erste Suche findet meist nur einen Teil der Handelsplaetze. Eine
    # zweite Suche nach dem Firmennamen bringt das US-Listing dazu - bei
    # 'NVD' etwa NVDA, das ueber das Kuerzel allein nicht auftaucht.
    names: list[str] = []
    for q in quotes:
        name = _clean_name(q.name)
        if name and _company_key(name) not in {_company_key(n) for n in names}:
            names.append(name)
    for name in names[:MAX_NAME_SEARCHES]:
        if _company_key(name) == _company_key(query):
            continue
        try:
            quotes += [q for q in search(name) if q.quote_type in ALLOWED_TYPES]
        except Exception as exc:  # die Namenssuche ist nur Ergaenzung
            log.info("Namenssuche '%s' fehlgeschlagen: %s", name, exc)

    companies: dict[str, Company] = {}
    seen_symbols: set[str] = set()
    wanted = {_company_key(n) for n in names}
    for q in quotes:
        name = _clean_name(q.name)
        key = _company_key(name)
        # Die Namenssuche bringt auch Verwandte ('Alibaba Health') - nur
        # Firmen behalten, die schon die erste Suche geliefert hat.
        if not name or key not in wanted or q.symbol in seen_symbols:
            continue
        seen_symbols.add(q.symbol)
        company = companies.setdefault(key, Company(name=name))
        company.listings.append(
            Listing(
                symbol=q.symbol,
                exchange=q.exchange or q.exchange_code,
                exchange_code=q.exchange_code,
                currency=CURRENCY.get(q.exchange_code),
            )
        )

    # Die Suche ist unscharf. Behalten wird, wessen Symbol zur Eingabe passt
    # oder wessen Name sie als ganzes Wort enthaelt; Symboltreffer zuerst.
    relevance = {key: _relevance(c, query, symbol) for key, c in companies.items()}
    ordered = [k for k in companies if relevance[k] is not None]
    ordered.sort(key=lambda k: relevance[k])          # stabil: Suchreihenfolge bleibt
    companies = {k: companies[k] for k in ordered[:MAX_COMPANIES]}

    for company in companies.values():
        company.listings.sort(key=_rank)
        del company.listings[MAX_LISTINGS_PER_COMPANY:]

    # Ohne Kurse kann die Pipeline mit einem Listing nichts anfangen - und
    # der Kurs ist die Plausibilitaetsprobe, die Namen allein nicht leisten.
    shown = [l.symbol for c in companies.values() for l in c.listings]
    try:
        last = prices(shown) if shown else {}
    except Exception as exc:
        log.warning("Kursprobe fehlgeschlagen: %s", exc)
        last = {}
    for company in companies.values():
        for listing in company.listings:
            if listing.symbol in last:
                listing.last_date, listing.last_close = last[listing.symbol]
        if last:
            company.listings = [l for l in company.listings if l.last_close is not None]

    result.companies = [c for c in companies.values() if c.listings]
    return result


def resolve_many(
    raw: str,
    search: Callable[[str], list[Quote]] | None = None,
    prices: Callable[[list[str]], dict[str, tuple[dt.date, float]]] | None = None,
) -> list[Lookup]:
    queries = split_queries(raw)
    search = search or search_quotes
    prices = prices or probe_prices
    if len(queries) <= 1:
        return [resolve(q, search, prices) for q in queries]
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
        return list(pool.map(lambda q: resolve(q, search, prices), queries))


# -- Yahoo --------------------------------------------------------------------


def search_quotes(query: str) -> list[Quote]:  # pragma: no cover - Netzzugriff
    import yfinance as yf

    found = yf.Search(query, max_results=10, news_count=0, raise_errors=True)
    out: list[Quote] = []
    for q in found.quotes or []:
        symbol = normalize_symbol(q.get("symbol") or "")
        if not symbol:
            continue
        out.append(
            Quote(
                symbol=symbol,
                name=q.get("longname") or q.get("shortname") or "",
                exchange_code=(q.get("exchange") or "").upper(),
                exchange=q.get("exchDisp") or q.get("exchange") or "",
                quote_type=(q.get("quoteType") or "").upper(),
            )
        )
    return out


def probe_prices(symbols: list[str]) -> dict[str, tuple[dt.date, float]]:  # pragma: no cover
    """Letzter Schlusskurs je Symbol, in einem einzigen Abruf.

    Ohne den Einzelabruf-Rueckfall von fetch_prices: fehlt ein exotisches
    Listing im Batch, faellt es eben weg, statt die Pruefung zu verzoegern.
    """
    from app.sources.prices import _download, _extract, _to_rows

    today = dt.date.today()
    frame = _download(symbols, today - dt.timedelta(days=10), today + dt.timedelta(days=1))
    last: dict[str, tuple[dt.date, float]] = {}
    for symbol in symbols:
        sub = _extract(frame, symbol, single=len(symbols) == 1)
        if sub is None or sub.empty:
            continue
        rows = _to_rows(symbol, sub)
        if rows:
            last[symbol] = (rows[-1].date, rows[-1].close)
    return last
