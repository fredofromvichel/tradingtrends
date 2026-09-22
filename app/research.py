"""Gezielte Recherche-Links je Titel.

Baut Suchanfragen, die auf kursrelevante Treffer eingegrenzt sind: das
Unternehmen selbst, Ereignisse mit Kurswirkung, die Branche und Ereignisse,
die die ganze Branche betreffen. Die Begriffslisten stehen in config.yaml und
lassen sich ohne Rebuild anpassen.

Frei von Datenbank- und Netzzugriffen (siehe tests/test_research.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

# Sehr kurze Kuerzel sind als Suchbegriff wertlos - "V" (Visa) oder "F" (Ford)
# treffen alles. Ab dieser Laenge wird das Symbol mit in die Suche genommen.
MIN_SYMBOL_LENGTH_FOR_SEARCH = 3

# Google News akzeptiert lange Anfragen, aber die Trefferqualitaet sinkt mit
# jeder zusaetzlichen ODER-Alternative.
MAX_TERMS_PER_GROUP = 14


@dataclass(frozen=True)
class ResearchLink:
    label: str
    url: str
    description: str
    # Frei von Fremddaten? Nur zur Anzeige der Gruppierung.
    group: str


def _or_group(terms: list[str], limit: int = MAX_TERMS_PER_GROUP) -> str:
    """Begriffe zu einer ODER-Gruppe verbinden, mehrwortige in Anfuehrungszeichen."""
    cleaned: list[str] = []
    for term in terms:
        term = str(term).strip()
        if not term or term in cleaned:
            continue
        cleaned.append(term)
        if len(cleaned) >= limit:
            break
    if not cleaned:
        return ""
    quoted = [f'"{t}"' if " " in t else t for t in cleaned]
    return "(" + " OR ".join(quoted) + ")"


def _news_url(query: str, days: int, locale: dict[str, str]) -> str:
    """Google-News-Suche mit Zeitfenster."""
    full = f"{query} when:{days}d".strip()
    params = (
        f"q={quote_plus(full)}"
        f"&hl={locale['hl']}&gl={locale['gl']}&ceid={quote_plus(locale['ceid'])}"
    )
    return f"https://news.google.com/search?{params}"


def _subject_terms(symbol: str, company: str | None) -> list[str]:
    terms: list[str] = []
    if company:
        terms.append(company)
        # "Apple Inc" -> zusaetzlich "Apple": Schlagzeilen nennen selten die
        # vollstaendige Firmierung.
        short = _strip_legal_form(company)
        if short and short != company:
            terms.append(short)
    if len(symbol) >= MIN_SYMBOL_LENGTH_FOR_SEARCH:
        terms.append(symbol)
    return terms or [symbol]


_LEGAL_FORMS = (
    "Inc.", "Inc", "Corporation", "Corp.", "Corp", "Co.", "Company",
    "PLC", "plc", "Ltd.", "Ltd", "Limited", "N.V.", "NV", "S.A.", "SA",
    "AG", "SE", "KGaA", "GmbH", "Holdings", "Holding", "Group", "The",
)


def _strip_legal_form(company: str) -> str:
    words = company.replace(",", " ").split()
    while words and words[-1] in _LEGAL_FORMS:
        words.pop()
    while words and words[0] in _LEGAL_FORMS:
        words.pop(0)
    return " ".join(words).strip()


def industry_terms(industry: str | None, mapping: dict[str, list[str]]) -> list[str]:
    """Uebersetzt die englische Finnhub-Branche in Suchbegriffe.

    Ohne Treffer in der Zuordnung wird die Rohbezeichnung genutzt - besser eine
    englische Suche als gar keine.
    """
    if not industry:
        return []
    key = industry.strip().lower()
    for name, terms in mapping.items():
        if str(name).strip().lower() == key:
            return [str(t) for t in terms]
    return [industry]


def build_research_links(
    symbol: str,
    company: str | None,
    industry: str | None,
    *,
    company_event_terms: list[str],
    sector_event_terms: list[str],
    analyst_terms: list[str],
    industry_mapping: dict[str, list[str]],
    locale: dict[str, str] | None = None,
    recent_days: int = 7,
    context_days: int = 30,
) -> list[ResearchLink]:
    """Erzeugt die Recherche-Links fuer einen Titel."""
    locale = locale or {"hl": "de", "gl": "DE", "ceid": "DE:de"}
    subject = _or_group(_subject_terms(symbol, company), limit=3)
    sector = _or_group(industry_terms(industry, industry_mapping))

    links: list[ResearchLink] = [
        ResearchLink(
            label=f"Nachrichten der letzten {recent_days} Tage",
            url=_news_url(subject, recent_days, locale),
            description="Alles zum Unternehmen, ohne thematische Einengung – "
                        "der Einstieg, wenn du wissen willst, was gerade los ist.",
            group="Unternehmen",
        ),
        ResearchLink(
            label="Kursrelevante Ereignisse",
            url=_news_url(
                f"{subject} {_or_group(company_event_terms)}".strip(),
                context_days,
                locale,
            ),
            description="Auf Meldungen eingegrenzt, die den Kurs typischerweise "
                        "bewegen: Zahlen, Prognosen, Übernahmen, Klagen, Rückrufe.",
            group="Unternehmen",
        ),
        ResearchLink(
            label="Analysten und Kursziele",
            url=_news_url(
                f"{subject} {_or_group(analyst_terms)}".strip(), context_days, locale
            ),
            description="Hoch- und Abstufungen, geänderte Kursziele. Fremdmeinung, "
                        "kein Bestandteil der PEAD-Logik.",
            group="Unternehmen",
        ),
    ]

    if sector:
        links.append(
            ResearchLink(
                label=f"Branche: {industry}",
                url=_news_url(sector, context_days, locale),
                description="Was in der Branche insgesamt passiert – oft die "
                            "Ursache, wenn mehrere Titel gleichzeitig laufen.",
                group="Branche",
            )
        )
        links.append(
            ResearchLink(
                label="Branchenereignisse mit Kurswirkung",
                url=_news_url(
                    f"{sector} {_or_group(sector_event_terms)}".strip(),
                    context_days,
                    locale,
                ),
                description="Regulierung, Zölle, Lieferketten, Nachfrage – "
                            "Themen, die die ganze Branche treffen.",
                group="Branche",
            )
        )

    links.append(
        ResearchLink(
            label="SEC-Pflichtmitteilungen (8-K)",
            url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&ticker={quote_plus(symbol)}&type=8-K&dateb=&owner=include&count=40",
            description="Meldepflichtige Ereignisse im Original, ohne "
                        "journalistische Zwischenstufe. Nur für US-Titel.",
            group="Primärquellen",
        )
    )
    links.append(
        ResearchLink(
            label="Kursprofil bei Yahoo Finance",
            url=f"https://finance.yahoo.com/quote/{quote_plus(symbol)}",
            description="Dieselbe Quelle, aus der die Kurse hier stammen – "
                        "zum Gegenprüfen der Zahlen.",
            group="Primärquellen",
        )
    )
    return links
