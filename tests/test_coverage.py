"""Tests des Gedächtnisses für Finnhub-Tarifsperren."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import app.db as db_module
from app.coverage import (
    blocked_symbols,
    coverage_note,
    is_due_for_recheck,
    record_block,
    record_success,
    should_skip,
)
from app.db import init_engine, session_scope

CAL = "/calendar/earnings"
PROF = "/stock/profile2"


@pytest.fixture
def database(tmp_path: Path):
    init_engine(tmp_path / "coverage.db")
    yield
    db_module._engine = None
    db_module._SessionLocal = None


# -- Wiedervorlage (reine Entscheidung) -------------------------------------


def test_frische_sperre_ist_noch_nicht_faellig():
    jetzt = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc)
    assert is_due_for_recheck(jetzt - dt.timedelta(days=3), 14, jetzt) is False


def test_alte_sperre_wird_erneut_geprueft():
    jetzt = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc)
    assert is_due_for_recheck(jetzt - dt.timedelta(days=20), 14, jetzt) is True


def test_zeitstempel_ohne_zone_wird_als_utc_gelesen():
    """SQLite gibt naive Zeitstempel zurück - sonst bricht der Vergleich."""
    jetzt = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc)
    naiv = dt.datetime(2026, 9, 1)          # ohne tzinfo
    assert is_due_for_recheck(naiv, 14, jetzt) is True


def test_unsinniges_intervall_wird_abgewiesen():
    with pytest.raises(ValueError):
        is_due_for_recheck(dt.datetime.now(dt.timezone.utc), 0)


# -- Sperren merken und überspringen ----------------------------------------


def test_ohne_eintrag_wird_nicht_uebersprungen(database):
    with session_scope() as session:
        assert should_skip(session, "AAPL", CAL, 14) is False


def test_gemerkte_sperre_wird_uebersprungen(database):
    with session_scope() as session:
        record_block(session, "AIR.DE", CAL)
    with session_scope() as session:
        assert should_skip(session, "AIR.DE", CAL, 14) is True


def test_sperre_gilt_nur_fuer_ihren_endpunkt(database):
    with session_scope() as session:
        record_block(session, "AIR.DE", CAL)
    with session_scope() as session:
        assert should_skip(session, "AIR.DE", PROF, 14) is False


def test_sperre_gilt_nur_fuer_ihr_symbol(database):
    with session_scope() as session:
        record_block(session, "AIR.DE", CAL)
    with session_scope() as session:
        assert should_skip(session, "AAPL", CAL, 14) is False


def test_alte_sperre_wird_einmal_erneut_probiert(database):
    """Sonst bliebe ein Tarif-Upgrade unbemerkt."""
    with session_scope() as session:
        record_block(session, "AIR.DE", CAL)
    # Sperre künstlich altern lassen.
    with session_scope() as session:
        from sqlalchemy import select

        from app.models import FinnhubCoverage

        row = session.scalar(select(FinnhubCoverage))
        row.last_checked = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)

    with session_scope() as session:
        assert should_skip(session, "AIR.DE", CAL, 14) is False
    # Der Versuch verschiebt die Wiedervorlage, sonst wäre sofort wieder fällig.
    with session_scope() as session:
        assert should_skip(session, "AIR.DE", CAL, 14) is True


def test_erfolg_hebt_die_sperre_auf(database):
    with session_scope() as session:
        record_block(session, "AIR.DE", CAL)
    with session_scope() as session:
        record_success(session, "AIR.DE", CAL)
    with session_scope() as session:
        assert should_skip(session, "AIR.DE", CAL, 14) is False
        assert blocked_symbols(session) == []


def test_erfolg_ohne_sperre_ist_folgenlos(database):
    with session_scope() as session:
        record_success(session, "AAPL", CAL)
        assert blocked_symbols(session) == []


def test_wiederholter_block_erzeugt_keinen_zweiten_eintrag(database):
    with session_scope() as session:
        record_block(session, "AIR.DE", CAL)
        record_block(session, "AIR.DE", CAL)
    with session_scope() as session:
        assert blocked_symbols(session) == ["AIR.DE"]


# -- Zusammengefasster Hinweis ----------------------------------------------


def test_ohne_sperren_gibt_es_keinen_hinweis(database):
    with session_scope() as session:
        assert coverage_note(session) is None


def test_hinweis_zaehlt_titel_nicht_fehlschlaege(database):
    """Der Fehler in der alten Meldung: zwei Endpunkte je Titel ergaben die
    doppelte Anzahl."""
    with session_scope() as session:
        for symbol in ("AIR.DE", "ENR.DE", "RWE.DE"):
            record_block(session, symbol, CAL)
            record_block(session, symbol, PROF)

    with session_scope() as session:
        note = coverage_note(session)
    assert note.startswith("3 Titel")
    assert "6" not in note.split(":")[0]
    assert "AIR.DE, ENR.DE, RWE.DE" in note
    assert "Momentum" in note      # sagt, was trotzdem funktioniert
