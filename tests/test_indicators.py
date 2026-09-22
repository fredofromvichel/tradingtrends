"""Tests der Kurskennzahlen - ohne Netz und ohne Datenbank."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from app.indicators import (
    PricePoint,
    compute_context,
    max_drawdown,
    range_position,
    realized_volatility,
    return_over,
    sma,
)


def series(values: list[float], start: dt.date = dt.date(2026, 1, 5)) -> list[PricePoint]:
    """Werktagsreihe aus Schlusskursen."""
    points: list[PricePoint] = []
    day = start
    for value in values:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        points.append(PricePoint(day, value, 1_000_000))
        day += dt.timedelta(days=1)
    return points


# -- Gleitender Durchschnitt ------------------------------------------------


def test_sma_mittelt_das_fenster():
    assert sma([1, 2, 3, 4, 5], 5) == pytest.approx(3.0)
    assert sma([1, 2, 3, 4, 5], 2) == pytest.approx(4.5)


def test_sma_none_bei_zu_wenig_daten():
    assert sma([1, 2], 5) is None


def test_sma_weist_unsinniges_fenster_ab():
    with pytest.raises(ValueError):
        sma([1, 2, 3], 0)


# -- Rendite ----------------------------------------------------------------


def test_return_over_rechnet_gegen_den_startwert():
    assert return_over([100, 105, 110], 2) == pytest.approx(0.10)
    assert return_over([100, 90], 1) == pytest.approx(-0.10)


def test_return_over_none_bei_zu_kurzer_reihe():
    assert return_over([100, 105], 5) is None


# -- Volatilität ------------------------------------------------------------


def test_volatilitaet_ist_bei_konstantem_kurs_null():
    assert realized_volatility([100.0] * 30) == pytest.approx(0.0, abs=1e-12)


def test_staerkere_schwankung_ergibt_hoehere_volatilitaet():
    ruhig = [100 + (1 if i % 2 else -1) * 0.2 for i in range(60)]
    unruhig = [100 + (1 if i % 2 else -1) * 4.0 for i in range(60)]
    assert realized_volatility(unruhig) > realized_volatility(ruhig)


def test_volatilitaet_ist_annualisiert():
    """Tägliche Streuung mal Wurzel(252) - eine bekannte Größenordnung."""
    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    vol = realized_volatility(closes)
    daily = math.log(1.01)
    assert vol == pytest.approx(daily * math.sqrt(252), rel=0.05)


def test_volatilitaet_none_bei_zu_wenig_daten():
    assert realized_volatility([100.0, 101.0]) is None


# -- Rückgang und Spanne ----------------------------------------------------


def test_max_drawdown_findet_den_tiefsten_ruecksetzer():
    assert max_drawdown([100, 120, 60, 90]) == pytest.approx(-0.5)


def test_max_drawdown_ist_null_bei_stetigem_anstieg():
    assert max_drawdown([100, 110, 120]) == pytest.approx(0.0)


def test_range_position_an_den_raendern_und_in_der_mitte():
    assert range_position(50, 50, 150) == pytest.approx(0.0)
    assert range_position(150, 50, 150) == pytest.approx(1.0)
    assert range_position(100, 50, 150) == pytest.approx(0.5)


def test_range_position_ohne_spanne():
    assert range_position(100, 100, 100) is None


# -- Gesamtkontext ----------------------------------------------------------


def test_kontext_ohne_daten_ist_leer():
    ctx = compute_context([])
    assert ctx.bars == 0
    assert ctx.last_close is None and ctx.sma50 is None


def test_kontext_fuellt_was_die_datenlage_hergibt():
    ctx = compute_context(series([100 + i for i in range(60)]))
    assert ctx.bars == 60
    assert ctx.last_close == pytest.approx(159.0)
    assert ctx.sma20 is not None
    assert ctx.sma50 is not None
    assert ctx.sma200 is None          # zu kurze Historie
    assert ctx.range_position == pytest.approx(1.0)   # am Hoch
    assert ctx.max_drawdown_52w == pytest.approx(0.0)


def test_kontext_erkennt_kurs_unter_dem_durchschnitt():
    steigend_dann_einbruch = [100 + i for i in range(50)] + [120] * 10
    ctx = compute_context(series(steigend_dann_einbruch))
    assert ctx.distance_to(ctx.sma20) is not None
    assert ctx.range_position is not None and ctx.range_position < 1.0
    assert ctx.max_drawdown_52w < 0


def test_volumenverhaeltnis_gegen_den_durchschnitt():
    points = series([100.0] * 25)
    points[-1] = PricePoint(points[-1].date, 100.0, 3_000_000)
    ctx = compute_context(points)
    assert ctx.volume_ratio is not None
    assert ctx.volume_ratio > 1.5
