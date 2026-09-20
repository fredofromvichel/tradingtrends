"""Die Schema-Ergaenzung darf bestehende Daten niemals antasten."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

import app.db as db_module
from app.db import init_engine, session_scope
from app.models import Ticker


@pytest.fixture
def reset_engine():
    yield
    db_module._engine = None
    db_module._SessionLocal = None


def _legacy_database(path: Path) -> None:
    """Eine poc.db, wie sie vor der Erweiterung um industry/exchange aussah."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE tickers ("
                "  symbol VARCHAR(16) NOT NULL PRIMARY KEY,"
                "  name VARCHAR(128),"
                "  active BOOLEAN NOT NULL"
                ")"
            )
        )
        conn.execute(
            text("INSERT INTO tickers (symbol, name, active) VALUES ('AAPL', 'Apple Inc', 1)")
        )
    engine.dispose()


def test_fehlende_spalten_werden_ergaenzt(tmp_path: Path, reset_engine):
    db = tmp_path / "legacy.db"
    _legacy_database(db)

    init_engine(db)

    columns = {c["name"] for c in inspect(db_module._engine).get_columns("tickers")}
    assert {"industry", "exchange"} <= columns


def test_bestehende_zeilen_ueberleben_die_ergaenzung(tmp_path: Path, reset_engine):
    db = tmp_path / "legacy.db"
    _legacy_database(db)

    init_engine(db)

    with session_scope() as session:
        row = session.get(Ticker, "AAPL")
        assert row is not None
        assert row.name == "Apple Inc"   # Altbestand unveraendert
        assert row.active is True
        assert row.industry is None      # neue Spalte, noch leer
        assert row.exchange is None


def test_fehlende_tabellen_werden_angelegt(tmp_path: Path, reset_engine):
    db = tmp_path / "legacy.db"
    _legacy_database(db)

    init_engine(db)

    tables = set(inspect(db_module._engine).get_table_names())
    assert {"tickers", "earnings_events", "prices", "signals", "pipeline_runs"} <= tables


def test_wiederholter_start_ist_folgenlos(tmp_path: Path, reset_engine):
    db = tmp_path / "legacy.db"
    _legacy_database(db)

    init_engine(db)
    before = {c["name"] for c in inspect(db_module._engine).get_columns("tickers")}

    db_module._engine = None
    db_module._SessionLocal = None
    init_engine(db)
    after = {c["name"] for c in inspect(db_module._engine).get_columns("tickers")}

    assert before == after
    with session_scope() as session:
        assert session.get(Ticker, "AAPL").name == "Apple Inc"
