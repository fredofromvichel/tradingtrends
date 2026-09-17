"""Tests der Signal-Logik - ohne Netz und ohne Datenbank."""

from __future__ import annotations

import datetime as dt

import pytest

from app.signals import (
    BUY,
    SELL,
    compute_return_pct,
    compute_sue,
    compute_surprise_pct,
    decide,
    find_entry_day,
    find_exit_day,
    trading_days_elapsed,
)

THRESHOLDS = dict(
    sue_threshold_buy=1.0,
    sue_threshold_sell=-1.0,
    surprise_pct_fallback_buy=0.05,
    surprise_pct_fallback_sell=-0.05,
)


# -- Surprise ---------------------------------------------------------------


def test_surprise_positiv():
    assert compute_surprise_pct(1.10, 1.00) == pytest.approx(0.10)


def test_surprise_negativ():
    assert compute_surprise_pct(0.90, 1.00) == pytest.approx(-0.10)


def test_surprise_bei_negativer_schaetzung_nutzt_betrag():
    # Verlusterwartung -0.50, tatsaechlich -0.40 -> positive Ueberraschung.
    assert compute_surprise_pct(-0.40, -0.50) == pytest.approx(0.20)


def test_surprise_faellt_auf_finnhub_wert_zurueck_wenn_schaetzung_null():
    assert compute_surprise_pct(0.10, 0.0, finnhub_surprise_percent=12.5) == pytest.approx(0.125)


def test_surprise_none_ohne_daten():
    assert compute_surprise_pct(None, None) is None
    assert compute_surprise_pct(1.0, 0.0) is None


# -- SUE --------------------------------------------------------------------


def test_sue_mit_ausreichender_historie():
    history = [0.01, 0.02, -0.01, 0.00]  # stdev ca. 0.01291
    sue = compute_sue(0.06, history, min_history=4)
    assert sue is not None
    assert sue == pytest.approx(0.06 / 0.0129099, rel=1e-3)


def test_sue_none_bei_zu_kurzer_historie():
    assert compute_sue(0.06, [0.01, 0.02, 0.03], min_history=4) is None


def test_sue_none_bei_streuung_null():
    assert compute_sue(0.06, [0.02, 0.02, 0.02, 0.02], min_history=4) is None


def test_sue_none_ohne_surprise():
    assert compute_sue(None, [0.01, 0.02, -0.01, 0.0]) is None


# -- Entscheidung -----------------------------------------------------------


def test_sue_ueber_schwelle_gibt_buy():
    d = decide(0.06, 1.5, **THRESHOLDS)
    assert d.signal_type == BUY and d.basis == "sue"


def test_sue_unter_schwelle_gibt_sell():
    d = decide(-0.06, -1.5, **THRESHOLDS)
    assert d.signal_type == SELL and d.basis == "sue"


def test_sue_innerhalb_der_schwellen_gibt_kein_signal():
    assert decide(0.20, 0.5, **THRESHOLDS).signal_type is None


def test_sue_hat_vorrang_vor_surprise():
    # Grosse Rohueberraschung, aber statistisch unauffaellig -> kein Signal.
    d = decide(0.30, 0.4, **THRESHOLDS)
    assert d.signal_type is None and d.basis == "sue"


def test_fallback_auf_surprise_ohne_sue():
    d = decide(0.07, None, **THRESHOLDS)
    assert d.signal_type == BUY and d.basis == "surprise_pct"

    d = decide(-0.07, None, **THRESHOLDS)
    assert d.signal_type == SELL and d.basis == "surprise_pct"

    assert decide(0.03, None, **THRESHOLDS).signal_type is None


def test_grenzwert_ist_exklusiv():
    # Genau auf der Schwelle loest noch kein Signal aus.
    assert decide(None, 1.0, **THRESHOLDS).signal_type is None
    assert decide(0.05, None, **THRESHOLDS).signal_type is None


def test_kein_signal_ohne_daten():
    d = decide(None, None, **THRESHOLDS)
    assert d.signal_type is None and d.basis == "none"


# -- Rendite ----------------------------------------------------------------


def test_rendite_buy():
    assert compute_return_pct(BUY, 100.0, 110.0) == pytest.approx(0.10)


def test_rendite_sell_ist_short():
    assert compute_return_pct(SELL, 100.0, 90.0) == pytest.approx(0.10)
    assert compute_return_pct(SELL, 100.0, 110.0) == pytest.approx(-0.10)


def test_rendite_bei_ungueltigem_einstieg():
    with pytest.raises(ValueError):
        compute_return_pct(BUY, 0.0, 10.0)


# -- Handelstags-Arithmetik -------------------------------------------------


def _days(n: int, start: dt.date = dt.date(2026, 1, 5)) -> list[dt.date]:
    """n aufeinanderfolgende Werktage ab start (Wochenenden ausgelassen)."""
    out: list[dt.date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def test_einstieg_am_meldetag_nur_bei_meldung_vor_handelsbeginn():
    dates = _days(10)
    assert find_entry_day(dates, dates[2], "bmo") == dates[2]


def test_einstieg_nach_boersenschluss_erst_am_folgetag():
    """Der Schlusskurs des Meldetages liegt vor der Nachricht - kein Lookahead."""
    dates = _days(10)
    assert find_entry_day(dates, dates[2], "amc") == dates[3]


def test_einstieg_waehrend_des_handels_erst_am_folgetag():
    dates = _days(10)
    assert find_entry_day(dates, dates[2], "dmh") == dates[3]


def test_unbekannter_meldezeitpunkt_wird_konservativ_behandelt():
    dates = _days(10)
    assert find_entry_day(dates, dates[2], None) == dates[3]


def test_einstieg_rueckt_auf_naechsten_handelstag():
    dates = _days(10)  # Mo 05.01. ff.
    samstag = dt.date(2026, 1, 10)
    assert find_entry_day(dates, samstag, "bmo") == dt.date(2026, 1, 12)  # Montag


def test_einstieg_none_wenn_noch_keine_kurse():
    dates = _days(5)
    assert find_entry_day(dates, dt.date(2030, 1, 1), "bmo") is None


def test_ausstieg_nach_genau_n_handelstagen():
    dates = _days(30)
    entry = dates[0]
    assert find_exit_day(dates, entry, 20) == dates[20]


def test_ausstieg_none_solange_kurse_fehlen():
    dates = _days(10)
    assert find_exit_day(dates, dates[0], 20) is None


def test_verstrichene_handelstage_zaehlt_einstiegstag_nicht_mit():
    dates = _days(10)
    assert trading_days_elapsed(dates, dates[0]) == 9
    assert trading_days_elapsed(dates, dates[9]) == 0
