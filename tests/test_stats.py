"""Tests der Statistik - der Teil, der Unsicherheit sichtbar machen soll."""

from __future__ import annotations

import pytest

from app.stats import (
    MIN_SAMPLE,
    estimate_mean,
    estimate_rate,
    t_critical_95,
    wilson_interval,
)


# -- Wilson-Intervall -------------------------------------------------------


def test_wilson_bei_kleiner_stichprobe_ist_sehr_breit():
    """3 von 4 Treffern sagen praktisch nichts aus - das muss man sehen."""
    low, high = wilson_interval(3, 4)
    assert low < 0.35
    assert high > 0.90
    assert high - low > 0.5


def test_wilson_wird_mit_wachsendem_n_schmaler():
    schmal = wilson_interval(750, 1000)
    breit = wilson_interval(3, 4)
    assert (schmal[1] - schmal[0]) < (breit[1] - breit[0])
    assert schmal[0] < 0.75 < schmal[1]


def test_wilson_bleibt_bei_extremen_anteilen_im_bereich():
    low, high = wilson_interval(0, 5)
    assert low == 0.0
    assert 0 < high < 1

    low, high = wilson_interval(5, 5)
    assert high == 1.0
    assert 0 < low < 1


def test_wilson_ohne_daten_laesst_alles_offen():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_weist_unmoegliche_eingaben_ab():
    with pytest.raises(ValueError):
        wilson_interval(5, 3)


# -- Punktschaetzer-Sperre --------------------------------------------------


def test_trefferquote_bleibt_unter_mindestgroesse_verborgen():
    est = estimate_rate(3, 4)
    assert est.point is None
    assert est.reliable is False
    # Das Intervall wird trotzdem ausgewiesen - es ist die Aussage.
    assert est.high - est.low > 0.5


def test_trefferquote_erscheint_ab_mindestgroesse():
    est = estimate_rate(12, MIN_SAMPLE)
    assert est.reliable is True
    assert est.point == pytest.approx(12 / MIN_SAMPLE)


# -- Mittelwert -------------------------------------------------------------


def test_mittelwert_bleibt_unter_mindestgroesse_verborgen():
    est = estimate_mean([0.05, 0.03, -0.01])
    assert est.mean is None
    assert est.reliable is False
    # Beschreibende Kennzahlen gibt es trotzdem.
    assert est.median == pytest.approx(0.03)
    assert est.minimum == pytest.approx(-0.01)
    assert est.maximum == pytest.approx(0.05)


def test_mittelwert_erscheint_ab_mindestgroesse():
    est = estimate_mean([0.02] * MIN_SAMPLE)
    assert est.reliable is True
    assert est.mean == pytest.approx(0.02)


def test_streuende_renditen_schliessen_die_null_nicht_aus():
    """Mal +10, mal -10 Prozent: kein von null unterscheidbarer Effekt."""
    values = [0.10, -0.10] * (MIN_SAMPLE // 2)
    est = estimate_mean(values)
    assert est.excludes_zero is False
    assert est.low < 0 < est.high


def test_konsistent_positive_renditen_schliessen_die_null_aus():
    values = [0.04, 0.05, 0.06] * (MIN_SAMPLE // 3 + 1)
    est = estimate_mean(values)
    assert est.excludes_zero is True
    assert est.low > 0


def test_einzelne_beobachtung_hat_kein_intervall():
    est = estimate_mean([0.07])
    assert est.n == 1
    assert est.low is None and est.high is None
    assert est.stdev is None
    assert est.excludes_zero is False


def test_leere_stichprobe():
    est = estimate_mean([])
    assert est.n == 0
    assert est.mean is None and est.median is None
    assert est.reliable is False


# -- t-Werte ----------------------------------------------------------------


def test_t_wert_faellt_mit_steigenden_freiheitsgraden():
    assert t_critical_95(1) > t_critical_95(10) > t_critical_95(30)


def test_t_wert_naehert_sich_der_normalverteilung():
    assert t_critical_95(500) == pytest.approx(1.96, abs=0.01)


def test_t_wert_braucht_mindestens_einen_freiheitsgrad():
    with pytest.raises(ValueError):
        t_critical_95(0)
