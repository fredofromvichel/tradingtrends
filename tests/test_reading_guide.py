"""Lesehilfe: Lesart je Wert und Reihenfolge nach Beleglage."""
import datetime as dt

import pytest

from app.indicators import PricePoint, compute_context
from app.reading_guide import EVIDENCE, build


def kontext(werte):
    tag = dt.date(2025, 1, 1)
    punkte = []
    for w in werte:
        while tag.weekday() >= 5:
            tag += dt.timedelta(days=1)
        punkte.append(PricePoint(tag, w, 1000))
        tag += dt.timedelta(days=1)
    return compute_context(punkte)


def nach_key(readings):
    return {r.key: r for r in readings}


def test_belastbares_steht_vorn():
    r = build(kontext([100 + i * 0.1 for i in range(300)]), rank=1, universe=20, score=0.3)
    stufen = [x.evidence.level for x in r]
    assert stufen == sorted(stufen, reverse=True)
    assert r[0].key == "momentum" and r[-1].key == "volume"


def test_aufwaertstrend_ueber_steigender_linie():
    r = nach_key(build(kontext([100 + i * 0.2 for i in range(300)])))
    assert "nach oben" in r["trend200"].reading
    assert "steigt ebenfalls" in r["trend200"].reading
    assert r["trend200"].value.startswith("+")


def test_junger_rueckgang_unter_noch_steigender_linie():
    werte = [100 + i * 0.3 for i in range(280)] + [184 - i * 3 for i in range(20)]
    r = nach_key(build(kontext(werte)))
    assert "nach unten" in r["trend200"].reading
    assert "Rückgang ist jung" in r["trend200"].reading


def test_kreuzung_heisst_nur_bestaetigung():
    r = nach_key(build(kontext([100 + i * 0.2 for i in range(300)])))
    assert "Golden Cross" in r["cross"].reading
    assert r["cross"].evidence.level == 1


@pytest.mark.parametrize("rang,erwartet", [(1, "long"), (20, "short"), (10, "hält sich heraus")])
def test_momentum_rang_und_gruppe(rang, erwartet):
    r = nach_key(build(kontext([100.0] * 300), rank=rang, universe=20, score=0.12))
    assert erwartet in r["momentum"].reading
    assert r["momentum"].value == f"Rang {rang} von 20"
    assert "Relativ gemeint" in r["momentum"].reading


def test_ohne_rang_keine_scheinaussage():
    r = nach_key(build(kontext([100.0] * 50)))
    assert r["momentum"].value == "–"


def test_nahe_am_hoch():
    r = nach_key(build(kontext([100 + i * 0.1 for i in range(300)])))
    assert "Nahe am Jahreshoch" in r["high52"].reading


def test_weit_unter_dem_hoch():
    werte = [100.0] * 200 + [60.0] * 100
    r = nach_key(build(kontext(werte)))
    assert "Weit unter" in r["high52"].reading
    assert r["high52"].value.startswith("−40")


def test_starker_monat_und_umkehr():
    werte = [100.0] * 279 + [100 + i * 1.5 for i in range(21)]
    r = nach_key(build(kontext(werte)))
    assert "sehr starker Monat" in r["reversal"].reading


def test_risiko_ueber_positionsgroesse_nicht_richtung():
    werte = [100 * (1.04 if i % 2 else 0.96) ** (i % 7) for i in range(300)]
    r = nach_key(build(kontext(werte)))
    assert "nichts über die Richtung" in r["risk"].reading
    assert "Rückgang" in r["risk"].value


def test_zu_kurze_reihe():
    r = nach_key(build(kontext([100.0, 101.0])))
    assert r["trend200"].value == "–" and r["cross"].value == "–"


def test_jede_nicht_rein_beschreibende_beleglage_nennt_quellen():
    for key, e in EVIDENCE.items():
        assert e.text
        if e.level > 0:
            assert e.sources, key


def test_zahlen_in_deutscher_schreibweise():
    r = nach_key(build(kontext([100 + i * 0.2 for i in range(300)])))
    assert "," in r["trend200"].value and "." not in r["trend200"].value
