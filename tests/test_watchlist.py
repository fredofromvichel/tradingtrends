"""Titelliste in der Datenbank: Startliste, Aufnehmen, Entfernen, Export."""
from pathlib import Path

import pytest

import app.db as db_module
from app import watchlist
from app.db import init_engine, session_scope
from app.models import Ticker


@pytest.fixture
def database(tmp_path: Path):
    init_engine(tmp_path / "wl.db")
    yield
    db_module._engine = None
    db_module._SessionLocal = None


def aktiv():
    with session_scope() as session:
        return watchlist.active_symbols(session)


def test_leere_db_uebernimmt_die_startliste(database):
    with session_scope() as session:
        res = watchlist.seed_tickers(session, ("MSFT", "AAPL"))
    assert res.seeded
    assert aktiv() == ["AAPL", "MSFT"]


def test_startliste_wirkt_nur_einmal(database):
    """Der Kern der Umstellung: ein git pull mit Platzhalterliste darf die
    eigene Liste nicht mehr ueberschreiben."""
    with session_scope() as session:
        watchlist.seed_tickers(session, ("NVDA", "AIR.DE"))
    with session_scope() as session:
        res = watchlist.seed_tickers(session, ("AAPL", "MSFT", "NVDA"))
    assert not res.seeded
    assert aktiv() == ["AIR.DE", "NVDA"]
    assert res.ignored == ("AAPL", "MSFT")


def test_leere_startliste_ist_erlaubt(database):
    with session_scope() as session:
        res = watchlist.seed_tickers(session, ())
    assert not res.seeded and aktiv() == []


def test_nur_inaktive_titel_zaehlen_als_befuellt(database):
    """Wer alles entfernt hat, soll nicht beim naechsten Start die Platzhalter
    zurueckbekommen."""
    with session_scope() as session:
        watchlist.seed_tickers(session, ("NVDA",))
    with session_scope() as session:
        watchlist.remove(session, "NVDA")
    with session_scope() as session:
        watchlist.seed_tickers(session, ("AAPL",))
    assert aktiv() == []


def test_aufnehmen_neu_reaktiviert_und_doppelt(database):
    with session_scope() as session:
        watchlist.seed_tickers(session, ("NVDA", "SAP"))
        watchlist.remove(session, "SAP")
    with session_scope() as session:
        res = watchlist.add(session, ["AOMD.DE", "SAP", "NVDA"], {"AOMD.DE": "Alstom SA"})
    assert res.added == ["AOMD.DE"]
    assert res.reactivated == ["SAP"]
    assert res.already == ["NVDA"]
    assert res.changed == ["AOMD.DE", "SAP"]
    with session_scope() as session:
        assert session.get(Ticker, "AOMD.DE").name == "Alstom SA"


def test_bekannter_name_wird_nicht_ueberschrieben(database):
    with session_scope() as session:
        session.add(Ticker(symbol="SAP", name="SAP SE", active=False))
    with session_scope() as session:
        watchlist.add(session, ["SAP"], {"SAP": "SAP SE Yahoo-Kurzname"})
    with session_scope() as session:
        assert session.get(Ticker, "SAP").name == "SAP SE"


def test_entfernen_behaelt_die_zeile(database):
    with session_scope() as session:
        watchlist.seed_tickers(session, ("NVDA",))
    with session_scope() as session:
        res = watchlist.remove(session, "NVDA")
    assert res.was_active and not res.has_open_positions
    with session_scope() as session:
        assert session.get(Ticker, "NVDA") is not None
        assert [t.symbol for t in watchlist.inactive_tickers(session)] == ["NVDA"]


def test_entfernen_unbekannter_titel(database):
    with session_scope() as session:
        assert watchlist.remove(session, "GIBTSNICHT") is None


def test_export_als_config_block(database):
    with session_scope() as session:
        session.add(Ticker(symbol="AOMD.DE", name="Alstom SA", active=True))
        session.add(Ticker(symbol="NVDA", name=None, active=True))
        session.add(Ticker(symbol="OLD", name="Weg", active=False))
    with session_scope() as session:
        block = watchlist.config_block(session)
    assert block == "tickers:\n  - AOMD.DE  # Alstom SA\n  - NVDA\n"


def test_export_ist_gueltiges_yaml(database):
    import yaml

    with session_scope() as session:
        session.add(Ticker(symbol="AIR.DE", name="Airbus SE: Luft- & Raumfahrt #1", active=True))
    with session_scope() as session:
        block = watchlist.config_block(session)
    assert yaml.safe_load(block) == {"tickers": ["AIR.DE"]}


def test_export_leer(database):
    with session_scope() as session:
        assert watchlist.config_block(session) == "tickers: []\n"


def test_us_titel_bekommt_keinen_yahoo_namen(database):
    """Sonst uebersprange der Lauf das Finnhub-Profil - und damit die Branche."""
    with session_scope() as session:
        watchlist.add(session, ["NVDA", "AOMD.DE"],
                      {"NVDA": "NVIDIA Corporation", "AOMD.DE": "Alstom SA"})
    with session_scope() as session:
        assert session.get(Ticker, "NVDA").name is None
        assert session.get(Ticker, "AOMD.DE").name == "Alstom SA"
