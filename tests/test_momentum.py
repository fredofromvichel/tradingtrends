"""Tests der Momentum-Logik - ohne Netz und ohne Datenbank."""

from __future__ import annotations

import pytest

from app.momentum import (
    LONG,
    SHORT,
    Basket,
    group_size,
    momentum_score,
    rank_universe,
    required_history,
)


# -- Momentum-Wert ----------------------------------------------------------


def test_score_misst_vom_fensteranfang_bis_zum_uebersprungenen_monat():
    # 300 Kurse, linear von 100 auf 399.
    closes = [100.0 + i for i in range(300)]
    score = momentum_score(closes, lookback_days=252, skip_days=21)
    start = closes[-253]          # 100 + 47 = 147
    end = closes[-22]             # 100 + 278 = 378
    assert score == pytest.approx((end - start) / start)


def test_uebersprungener_monat_wird_wirklich_ausgelassen():
    """Ein Einbruch in den letzten Tagen darf den Wert nicht beruehren."""
    closes = [100.0 + i for i in range(300)]
    ohne_einbruch = momentum_score(closes, 252, 21)

    mit_einbruch = list(closes)
    for i in range(1, 21):        # die letzten 20 Tage halbieren
        mit_einbruch[-i] = 50.0
    assert momentum_score(mit_einbruch, 252, 21) == pytest.approx(ohne_einbruch)


def test_ohne_ueberspringen_zaehlt_der_letzte_kurs():
    closes = [100.0 + i for i in range(300)]
    score = momentum_score(closes, lookback_days=252, skip_days=0)
    assert score == pytest.approx((closes[-1] - closes[-253]) / closes[-253])


def test_fallender_kurs_ergibt_negatives_momentum():
    closes = [400.0 - i for i in range(300)]
    assert momentum_score(closes, 252, 21) < 0


def test_score_none_bei_zu_kurzer_historie():
    """Stillschweigend ein kuerzeres Fenster zu nehmen waere eine andere Kennzahl."""
    assert momentum_score([100.0 + i for i in range(252)], 252, 21) is None
    assert momentum_score([100.0 + i for i in range(253)], 252, 21) is not None


def test_required_history_passt_zur_score_grenze():
    needed = required_history(252)
    assert momentum_score([100.0] * (needed - 1), 252, 21) is None
    assert momentum_score([100.0 + i for i in range(needed)], 252, 21) is not None


def test_score_weist_unsinnige_parameter_ab():
    closes = [100.0 + i for i in range(300)]
    with pytest.raises(ValueError):
        momentum_score(closes, lookback_days=21, skip_days=21)
    with pytest.raises(ValueError):
        momentum_score(closes, lookback_days=252, skip_days=-1)
    with pytest.raises(ValueError):
        momentum_score(closes, lookback_days=1, skip_days=0)


# -- Gruppengroesse ---------------------------------------------------------


def test_gruppengroesse_folgt_dem_anteil():
    assert group_size(20, 0.3) == 6
    assert group_size(20, 0.2) == 4


def test_gruppen_ueberschneiden_sich_nie():
    for n in range(4, 40):
        for fraction in (0.1, 0.25, 0.3, 0.45):
            assert 2 * group_size(n, fraction) <= n


def test_gruppengroesse_ist_mindestens_eins():
    assert group_size(8, 0.1) == 1


def test_gruppengroesse_weist_unsinnigen_anteil_ab():
    with pytest.raises(ValueError):
        group_size(20, 0.5)
    with pytest.raises(ValueError):
        group_size(20, 0.0)


# -- Rangfolge --------------------------------------------------------------


def scores(n: int = 12) -> dict[str, float]:
    """Titel T01..Tnn mit absteigendem Momentum."""
    return {f"T{i:02d}": (n - i) / 100 for i in range(1, n + 1)}


def test_spitze_wird_long_schluss_wird_short():
    basket = rank_universe(scores(12), fraction=0.25, min_universe=8)
    assert basket.group_size == 3
    assert [r.symbol for r in basket.longs] == ["T01", "T02", "T03"]
    assert [r.symbol for r in basket.shorts] == ["T10", "T11", "T12"]
    assert all(r.direction == LONG for r in basket.longs)
    assert all(r.direction == SHORT for r in basket.shorts)


def test_raenge_beginnen_bei_eins_und_laufen_durch():
    basket = rank_universe(scores(12), fraction=0.25, min_universe=8)
    alle = sorted(basket.all_ranked, key=lambda r: r.rank)
    assert [r.rank for r in alle] == list(range(1, 13))
    assert alle[0].symbol == "T01"


def test_mittelfeld_bleibt_ohne_richtung():
    basket = rank_universe(scores(12), fraction=0.25, min_universe=8)
    assert len(basket.middle) == 6
    assert all(r.direction is None for r in basket.middle)


def test_kein_titel_ist_gleichzeitig_long_und_short():
    for n in (8, 9, 12, 20, 21):
        basket = rank_universe(scores(n), fraction=0.3, min_universe=8)
        longs = {r.symbol for r in basket.longs}
        shorts = {r.symbol for r in basket.shorts}
        assert not (longs & shorts), n


def test_titel_ohne_wert_fallen_aus_dem_universum():
    werte: dict[str, float | None] = dict(scores(10))
    werte["NEU"] = None
    basket = rank_universe(werte, fraction=0.3, min_universe=8)
    assert basket.universe_size == 10
    assert "NEU" not in {r.symbol for r in basket.all_ranked}


def test_zu_kleines_universum_ergibt_keinen_korb():
    basket = rank_universe(scores(5), fraction=0.3, min_universe=8)
    assert basket.is_empty
    assert "5" in basket.reason and "8" in basket.reason


def test_gleichstand_wird_stabil_aufgeloest():
    """Derselbe Datenstand muss denselben Korb ergeben."""
    gleich = {f"T{i:02d}": 0.10 for i in range(1, 11)}
    erster = rank_universe(gleich, fraction=0.3, min_universe=8)
    zweiter = rank_universe(dict(reversed(list(gleich.items()))), fraction=0.3, min_universe=8)
    assert [r.symbol for r in erster.longs] == [r.symbol for r in zweiter.longs]
    assert [r.symbol for r in erster.shorts] == [r.symbol for r in zweiter.shorts]


def test_reihenfolge_der_eingabe_aendert_das_ergebnis_nicht():
    original = scores(12)
    gemischt = dict(sorted(original.items(), key=lambda kv: kv[0], reverse=True))
    assert [r.symbol for r in rank_universe(original, 0.25, 8).longs] == \
           [r.symbol for r in rank_universe(gemischt, 0.25, 8).longs]


def test_negatives_momentum_kann_trotzdem_long_sein():
    """In einem fallenden Markt besteht die Spitze aus den kleinsten Verlusten."""
    alles_negativ = {f"T{i:02d}": -0.05 * i for i in range(1, 11)}
    basket = rank_universe(alles_negativ, fraction=0.3, min_universe=8)
    assert basket.longs[0].symbol == "T01"
    assert basket.longs[0].score < 0        # long trotz Verlust
    assert basket.shorts[-1].score < basket.longs[0].score


def test_leeres_universum():
    basket = rank_universe({}, fraction=0.3, min_universe=8)
    assert basket.is_empty
    assert basket.universe_size == 0
