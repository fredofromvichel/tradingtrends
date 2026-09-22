"""Prueft die Verdrahtung der Rechenweg-Bausteine.

Der Rechenweg steckt in drei Dateien: die Kette selbst (_explain_body.html),
der aufgeklappte Block fuer Seitenkoepfe (_explain.html) und die Tabellenzeile
ueber die volle Breite (_explain_row.html). Wer eine Tabelle umbaut und den
Aufklapp-Knopf vergisst, versteckt den Rechenweg unerreichbar - genau das
faengt dieses Modul ab.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from app.explain import Explanation, Step

VORLAGEN = Path(__file__).resolve().parents[1] / "app" / "templates"


@pytest.fixture(scope="module")
def env():
    return Environment(loader=FileSystemLoader(str(VORLAGEN)), autoescape=True)


@pytest.fixture
def erklaerung():
    return Explanation(
        headline="Warum BUY?",
        steps=[
            Step("Die Meldung", "Am 04.08.2026 meldete das Unternehmen 0,54 je Aktie."),
            Step("Warum BUY", "SUE 1,08 liegt ueber der Schwelle.", "SUE 1,08 > 1,00", "good"),
        ],
        caveat="Alle Renditen sind brutto.",
    )


def tabellen_vorlagen():
    return [p for p in VORLAGEN.glob("*.html") if "_explain_row.html" in p.read_text()]


def test_es_gibt_ueberhaupt_tabellen_mit_rechenweg():
    """Ohne diese Zusicherung liefe der naechste Test leer durch."""
    assert tabellen_vorlagen()


@pytest.mark.parametrize("vorlage", tabellen_vorlagen(), ids=lambda p: p.name)
def test_jede_rechenweg_zeile_hat_einen_knopf(vorlage):
    text = vorlage.read_text()
    assert text.count("data-explain") == text.count('include "_explain_row.html"')


def test_kette_steht_nur_einmal_im_quelltext():
    """Zwei Kopien driften auseinander; gerendert wird dann je nach Seite anderes."""
    quellen = [p for p in VORLAGEN.glob("*.html") if 'class="explain-steps"' in p.read_text()]
    assert [p.name for p in quellen] == ["_explain_body.html"]


def test_rahmen_binden_die_kette_ein():
    for name in ("_explain.html", "_explain_row.html"):
        assert '_explain_body.html' in (VORLAGEN / name).read_text()


def test_zeile_spannt_ueber_die_ganze_tabelle(env, erklaerung):
    html = env.get_template("_explain_row.html").render(e=erklaerung)
    assert 'class="explain-row"' in html
    assert "colspan=" in html
    assert "Die Meldung" in html and "SUE 1,08 &gt; 1,00" in html


def test_block_ist_aufgeklappt(env, erklaerung):
    """Oben auf der Titelseite traegt der Rechenweg die Hauptaussage."""
    html = env.get_template("_explain.html").render(e=erklaerung)
    assert "<details class=\"explain\" open>" in html


def test_tonfall_landet_als_klasse(env, erklaerung):
    html = env.get_template("_explain_row.html").render(e=erklaerung)
    assert "tone-good" in html and "tone-neutral" in html


def test_rechenweg_ohne_formel_bleibt_leise(env):
    schlicht = Explanation(headline="x", steps=[Step("A", "B")])
    html = env.get_template("_explain_row.html").render(e=schlicht)
    assert "explain-formula" not in html
    assert "explain-caveat" not in html


def test_sortierung_haelt_die_paare_zusammen():
    """Sortieren nach Rendite darf den Rechenweg nicht an eine fremde Zeile haengen."""
    js = (VORLAGEN.parent / "static" / "ui.js").read_text()
    assert "gruppiere" in js
    assert "explain-row" in js
