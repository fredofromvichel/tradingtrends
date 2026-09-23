"""Tests der Recherche-Links."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote_plus, urlparse

import pytest

from app.research import _strip_legal_form, build_research_links, industry_terms

TERMS = dict(
    company_event_terms=["Quartalszahlen", "Übernahme", "profit warning"],
    sector_event_terms=["Regulierung", "Zölle"],
    analyst_terms=["Kursziel", "downgrade"],
    industry_mapping={"Technology": ["Technologiebranche", "tech sector"]},
)


def query_of(url: str) -> str:
    return unquote_plus(parse_qs(urlparse(url).query)["q"][0])


def links_for(symbol="AAPL", company="Apple Inc", industry="Technology", **kw):
    return build_research_links(symbol, company, industry, **{**TERMS, **kw})


# -- Firmenname -------------------------------------------------------------


def test_rechtsform_wird_fuer_die_suche_entfernt():
    assert _strip_legal_form("Apple Inc") == "Apple"
    assert _strip_legal_form("Deutsche Bank AG") == "Deutsche Bank"
    assert _strip_legal_form("The Walt Disney Company") == "Walt Disney"


def test_firmenname_ohne_rechtsform_bleibt_unveraendert():
    assert _strip_legal_form("Alphabet") == "Alphabet"


def test_suche_nutzt_langen_und_kurzen_firmennamen():
    query = query_of(links_for()[0].url)
    assert '"Apple Inc"' in query
    assert "Apple" in query


# -- Symbol -----------------------------------------------------------------


def test_kurzes_symbol_wird_nicht_mitgesucht():
    """'V' fuer Visa traefe alles - das Kuerzel bleibt draussen."""
    query = query_of(links_for(symbol="V", company="Visa Inc")[0].url)
    assert "Visa" in query
    assert " V " not in query and '"V"' not in query


def test_langes_symbol_wird_mitgesucht():
    assert "AAPL" in query_of(links_for()[0].url)


# -- Aufbau der Anfragen ----------------------------------------------------


def test_zeitfenster_steckt_in_der_anfrage():
    links = links_for(recent_days=7, context_days=30)
    assert "when:7d" in query_of(links[0].url)
    assert "when:30d" in query_of(links[1].url)


def test_ereignissuche_kombiniert_unternehmen_und_begriffe():
    query = query_of(links_for()[1].url)
    assert "Apple" in query
    assert "Quartalszahlen" in query
    assert '"profit warning"' in query      # mehrwortig, also in Anführungszeichen
    assert " OR " in query


def test_branchenbegriffe_werden_uebersetzt():
    assert industry_terms("Technology", TERMS["industry_mapping"]) == [
        "Technologiebranche", "tech sector"
    ]


def test_unbekannte_branche_wird_unveraendert_genutzt():
    assert industry_terms("Widgets", TERMS["industry_mapping"]) == ["Widgets"]


def test_ohne_branche_entfallen_die_branchenlinks():
    labels = [l.label for l in links_for(industry=None)]
    assert not any("Branche" in l for l in labels)
    groups = {l.group for l in links_for(industry=None)}
    assert "Branche" not in groups


def test_mit_branche_gibt_es_branche_und_branchenereignisse():
    groups = [l.group for l in links_for()]
    assert groups.count("Branche") == 2


# -- Zielseiten -------------------------------------------------------------


def test_alle_links_sind_absolute_https_adressen():
    for link in links_for():
        assert link.url.startswith("https://"), link.label
        assert link.label and link.description


def test_sec_link_enthaelt_das_symbol():
    sec = [l for l in links_for() if "SEC" in l.label][0]
    assert "ticker=AAPL" in sec.url


def test_sprache_und_region_sind_konfigurierbar():
    link = links_for(locale={"hl": "en", "gl": "US", "ceid": "US:en"})[0]
    params = parse_qs(urlparse(link.url).query)
    assert params["hl"] == ["en"]
    assert params["ceid"] == ["US:en"]


def test_begriffsliste_wird_begrenzt():
    """Zu viele ODER-Alternativen verwaessern das Ergebnis."""
    query = query_of(links_for(company_event_terms=[f"Begriff{i}" for i in range(40)])[1].url)
    assert query.count(" OR ") < 25


def test_leere_begriffsliste_bricht_nicht():
    links = links_for(company_event_terms=[], sector_event_terms=[], analyst_terms=[])
    assert all(l.url.startswith("https://") for l in links)


@pytest.mark.parametrize("yahoo", ["Utilities—Diversified", "Utilities – Diversified",
                                   "utilities-diversified", "Utilities - Diversified"])
def test_strichvarianten_der_branche(yahoo):
    from app.research import industry_terms

    mapping = {"Utilities - Diversified": ["Versorger"]}
    assert industry_terms(yahoo, mapping) == ["Versorger"]


@pytest.mark.parametrize("voll,kurz", [
    ("RWE Aktiengesellschaft", "RWE"),
    ("TKMS AG & Co KGaA", "TKMS"),
    ("Dassault Aviation société anonyme", "Dassault Aviation"),
    ("Carl Zeiss Meditec AG", "Carl Zeiss Meditec"),
    ("NVIDIA Corporation", "NVIDIA"),
])
def test_kurzname_fuer_suchen(voll, kurz):
    from app.research import short_name

    assert short_name(voll) == kurz
