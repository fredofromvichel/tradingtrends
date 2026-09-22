"""Tests des Rechenwegs - reine Textbildung, ohne Netz und Datenbank."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.explain import explain_momentum, explain_pead


def settings(**over):
    basis = dict(
        sue_threshold_buy=1.0,
        sue_threshold_sell=-1.0,
        surprise_pct_fallback_buy=0.05,
        surprise_pct_fallback_sell=-0.05,
        momentum=SimpleNamespace(lookback_days=252, skip_days=21),
    )
    basis.update(over)
    return SimpleNamespace(**basis)


def event(**over):
    basis = dict(
        eps_estimate=0.38, eps_actual=0.54, surprise_stdev=0.39,
        history_count=4, report_hour=None,
    )
    basis.update(over)
    return SimpleNamespace(**basis)


def signal(**over):
    basis = dict(
        signal_type="BUY", status="CLOSED",
        trigger_date=dt.date(2026, 8, 4), entry_date=dt.date(2026, 8, 5),
        entry_price=27.45, exit_date=dt.date(2026, 9, 2), exit_price=29.81,
        holding_period_days=20, return_pct=0.0859, benchmark_return_pct=0.021,
        sue_at_signal=1.08, surprise_pct_at_signal=0.4211,
    )
    basis.update(over)
    return SimpleNamespace(**basis)


def text_of(erklaerung) -> str:
    return " ".join(s.text + " " + (s.formula or "") for s in erklaerung.steps)


# -- PEAD -------------------------------------------------------------------


def test_meldung_nennt_beide_zahlen():
    t = text_of(explain_pead(signal(), event(), settings()))
    assert "0,54" in t and "0,38" in t


def test_ueberraschung_wird_als_rechnung_gezeigt():
    e = explain_pead(signal(), event(), settings())
    schritt = [s for s in e.steps if s.label == "Die Überraschung"][0]
    assert "÷" in schritt.formula
    assert "42,1" in schritt.formula


def test_sue_erklaert_die_eigene_schwankung_des_unternehmens():
    """Der Kern für Laien: 42 % sind viel oder wenig - je nach Unternehmen."""
    e = explain_pead(signal(), event(), settings())
    schritt = [s for s in e.steps if s.label == "Ist das viel?"][0]
    assert "39,0 %" in schritt.text        # die übliche Schwankung
    assert "4" in schritt.text             # aus wie vielen Quartalen
    assert "SUE 1,08" in schritt.formula


def test_ohne_historie_wird_der_fallback_erklaert():
    e = explain_pead(signal(sue_at_signal=None), event(surprise_stdev=None), settings())
    schritt = [s for s in e.steps if s.label == "Ist das viel?"][0]
    assert "zu wenige" in schritt.text
    warum = [s for s in e.steps if s.label.startswith("Warum")][0]
    assert "Ersatzschwelle" in warum.text


def test_schwelle_wird_benannt():
    e = explain_pead(signal(), event(), settings())
    warum = [s for s in e.steps if s.label == "Warum BUY"][0]
    assert "1,00" in warum.formula and ">" in warum.formula


def test_sell_nennt_die_untere_schwelle():
    e = explain_pead(
        signal(signal_type="SELL", sue_at_signal=-1.81, surprise_pct_at_signal=-0.38),
        event(eps_actual=0.31, eps_estimate=0.50), settings(),
    )
    warum = [s for s in e.steps if s.label == "Warum SELL"][0]
    assert "<" in warum.formula
    assert "-1,00" in warum.formula


def test_einstieg_am_folgetag_wird_begruendet():
    """Der Punkt, den man sonst für einen Fehler hält."""
    e = explain_pead(signal(), event(report_hour=None), settings())
    schritt = [s for s in e.steps if s.label == "Einstieg"][0]
    assert "nicht bekannt" in schritt.text
    assert "vor der Nachricht" in schritt.text


def test_meldung_nach_boersenschluss_wird_benannt():
    e = explain_pead(signal(), event(report_hour="amc"), settings())
    schritt = [s for s in e.steps if s.label == "Einstieg"][0]
    assert "nach Börsenschluss" in schritt.text


def test_meldung_vor_handelsbeginn_erlaubt_denselben_tag():
    e = explain_pead(
        signal(entry_date=dt.date(2026, 8, 4)), event(report_hour="bmo"), settings()
    )
    schritt = [s for s in e.steps if s.label == "Einstieg"][0]
    assert "bereits nach der Nachricht" in schritt.text


def test_short_erklaert_die_umgekehrte_rechnung():
    e = explain_pead(
        signal(signal_type="SELL", exit_price=25.0, return_pct=0.089,
               sue_at_signal=-1.8, surprise_pct_at_signal=-0.3),
        event(), settings(),
    )
    schritt = [s for s in e.steps if s.label == "Ausstieg"][0]
    assert "Leerverkauf" in schritt.text
    # Bei Short steht der Einstieg vorn in der Rechnung.
    assert schritt.formula.startswith("(27,45")


# -- Vergleichsmaßstab ------------------------------------------------------


def test_ueberrendite_wird_ausgewiesen():
    e = explain_pead(signal(), event(), settings())
    schritt = [s for s in e.steps if s.label == "Einordnung"][0]
    assert "2,10 %" in schritt.text            # der Vergleichswert
    assert "besser" in schritt.text
    assert "−" in schritt.formula


def test_gewinn_unter_dem_markt_wird_als_solcher_benannt():
    """Wichtig fürs Lernen: +3 % bei +8 % Markt ist kein Erfolg."""
    e = explain_pead(signal(return_pct=0.03, benchmark_return_pct=0.08), event(), settings())
    schritt = [s for s in e.steps if s.label == "Einordnung"][0]
    assert "schlechter" in schritt.text
    assert "eher vom Markt" in schritt.text


def test_ohne_vergleichswert_wird_das_gesagt():
    e = explain_pead(signal(benchmark_return_pct=None), event(), settings())
    schritt = [s for s in e.steps if s.label == "Einordnung"][0]
    assert "kein Vergleichswert" in schritt.text


def test_offenes_signal_hat_keinen_ausstieg():
    e = explain_pead(
        signal(status="OPEN", exit_date=None, exit_price=None, return_pct=None),
        event(), settings(),
    )
    labels = [s.label for s in e.steps]
    assert "Ausstieg" not in labels
    assert "Einordnung" not in labels
    assert "Einstieg" in labels


def test_hinweis_auf_bruttorenditen():
    e = explain_pead(signal(), event(), settings())
    assert "brutto" in e.caveat


# -- Momentum ---------------------------------------------------------------


def momentum_signal(**over):
    basis = dict(
        direction="LONG", rank=1, momentum_score=2.007,
        entry_date=dt.date(2026, 9, 21), entry_price=615.36,
        exit_date=None, exit_price=None, holding_period_days=21,
        return_pct=None, benchmark_return_pct=None,
    )
    basis.update(over)
    return SimpleNamespace(**basis)


def rebalance(**over):
    basis = dict(
        rebalance_date=dt.date(2026, 9, 22), universe_size=22, group_size=7,
        formation_start=dt.date(2025, 9, 18), formation_end=dt.date(2026, 8, 20),
        skip_days=21,
    )
    basis.update(over)
    return SimpleNamespace(**basis)


def test_momentum_nennt_fenster_und_universum():
    t = text_of(explain_momentum(momentum_signal(), rebalance(), settings()))
    assert "22" in t
    assert "18.09.2025" in t and "20.08.2026" in t


def test_momentum_erklaert_den_ausgesparten_monat():
    e = explain_momentum(momentum_signal(), rebalance(), settings())
    schritt = [s for s in e.steps if "letzte Monat" in s.label][0]
    assert "Gegenbewegung" in schritt.text


def test_momentum_betont_die_relativitaet():
    """Ohne diesen Hinweis liest man einen LONG als 'gute Aktie'."""
    t = text_of(explain_momentum(momentum_signal(), rebalance(), settings()))
    assert "relative" in t
    assert "kleinsten Verlusten" in t


def test_momentum_short_wird_als_leerverkauf_benannt():
    t = text_of(explain_momentum(momentum_signal(direction="SHORT", rank=22), rebalance(), settings()))
    assert "leerverkauft" in t
    assert "Schlussgruppe" in t


def test_momentum_ohne_umschichtungsdaten_bricht_nicht():
    e = explain_momentum(momentum_signal(), None, settings())
    assert e.steps
    assert any("Rang 1" in (s.formula or "") for s in e.steps)


def test_momentum_caveat_nennt_die_branchenrotation():
    e = explain_momentum(momentum_signal(), rebalance(), settings())
    assert "Branchenrotation" in e.caveat


# -- Sprachliche Genauigkeit ------------------------------------------------


def test_leerverkauf_wird_nicht_als_kauf_bezeichnet():
    e = explain_pead(
        signal(signal_type="SELL", sue_at_signal=-1.8, surprise_pct_at_signal=-0.38),
        event(), settings(),
    )
    schritt = [s for s in e.steps if s.label == "Einstieg"][0]
    assert schritt.text.startswith("Leerverkauft")
    assert "Gekauft" not in schritt.text


def test_kauf_heisst_gekauft():
    e = explain_pead(signal(), event(), settings())
    assert [s for s in e.steps if s.label == "Einstieg"][0].text.startswith("Gekauft")


def test_datum_in_deutscher_schreibweise():
    e = explain_pead(signal(), event(), settings())
    assert "04.08.2026" in e.steps[0].text
    assert "2026-08-04" not in e.steps[0].text


def test_differenz_ohne_doppeltes_vorzeichen():
    """'+15,07 % − -1,70 %' liest sich niemand."""
    e = explain_pead(signal(benchmark_return_pct=-0.017), event(), settings())
    formel = [s for s in e.steps if s.label == "Einordnung"][0].formula
    assert "− -" not in formel
    assert "+ 1,70 %" in formel


def test_positiver_vergleichswert_wird_abgezogen():
    e = explain_pead(signal(benchmark_return_pct=0.021), event(), settings())
    formel = [s for s in e.steps if s.label == "Einordnung"][0].formula
    assert "− 2,10 %" in formel
