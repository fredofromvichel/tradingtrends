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


def _drop_momentum_counters(path: Path) -> None:
    """pipeline_runs auf den Stand vor den Momentum-Zählern zurückbauen."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        ALTER TABLE pipeline_runs RENAME TO pr_alt;
        CREATE TABLE pipeline_runs (
          id INTEGER NOT NULL PRIMARY KEY,
          started_at DATETIME NOT NULL, finished_at DATETIME,
          trigger VARCHAR(16) NOT NULL, status VARCHAR(16) NOT NULL,
          events_ingested INTEGER NOT NULL, prices_ingested INTEGER NOT NULL,
          signals_opened INTEGER NOT NULL, signals_closed INTEGER NOT NULL,
          message VARCHAR(2000));
        INSERT INTO pipeline_runs
          SELECT id, started_at, finished_at, trigger, status, events_ingested,
                 prices_ingested, signals_opened, signals_closed, message
          FROM pr_alt;
        DROP TABLE pr_alt;
        """
    )
    conn.commit()
    conn.close()


def test_not_null_spalte_mit_festem_default_wird_ergaenzt(tmp_path: Path, reset_engine):
    """Der Fehler, der jede Seite mit 500 beantwortete: NOT-NULL-Spalten wurden
    übersprungen, danach schlug jede Abfrage mit 'no such column' fehl."""
    import datetime as dt

    from app.models import PipelineRun

    db = tmp_path / "alt.db"
    init_engine(db)
    with session_scope() as session:
        session.add(
            PipelineRun(
                started_at=dt.datetime.now(dt.timezone.utc), trigger="manual", status="OK"
            )
        )

    db_module._engine = None
    db_module._SessionLocal = None
    _drop_momentum_counters(db)

    init_engine(db)

    columns = {c["name"] for c in inspect(db_module._engine).get_columns("pipeline_runs")}
    assert {"momentum_opened", "momentum_closed"} <= columns

    # Und die Abfrage, die vorher scheiterte, läuft wieder.
    with session_scope() as session:
        run = session.scalars(__import__("sqlalchemy").select(PipelineRun)).first()
        assert run is not None
        assert run.momentum_opened == 0     # Default für Altbestände
        assert run.momentum_closed == 0
        assert run.trigger == "manual"      # Altdaten unverändert


def test_nicht_ergaenzbare_spalte_bricht_mit_klarer_meldung_ab(tmp_path: Path, reset_engine):
    """Lieber ein lesbarer Abbruch beim Start als 'no such column' auf jeder Seite."""
    from sqlalchemy import Integer

    from app.db import SchemaOutOfDate
    from app.models import Base, PipelineRun

    db = tmp_path / "alt.db"
    init_engine(db)
    db_module._engine = None
    db_module._SessionLocal = None

    # Eine NOT-NULL-Spalte ohne festen Default ans Modell hängen.
    tabelle = Base.metadata.tables["pipeline_runs"]
    spalte = __import__("sqlalchemy").Column("ohne_default", Integer, nullable=False)
    tabelle.append_column(spalte)
    try:
        with pytest.raises(SchemaOutOfDate) as exc:
            init_engine(db)
        assert "ohne_default" in str(exc.value)
        assert "make reset" in str(exc.value)
    finally:
        tabelle._columns.remove(spalte)


def test_default_literale_werden_korrekt_gebildet():
    from sqlalchemy import Boolean, Column, Float, Integer, String

    from app.db import _default_literal

    assert _default_literal(Column("a", Integer, default=0)) == "0"
    assert _default_literal(Column("b", Float, default=1.5)) == "1.5"
    assert _default_literal(Column("c", Boolean, default=True)) == "1"
    assert _default_literal(Column("d", Boolean, default=False)) == "0"
    assert _default_literal(Column("e", String, default="OPEN")) == "'OPEN'"
    # Anführungszeichen im Wert dürfen das Literal nicht sprengen.
    assert _default_literal(Column("f", String, default="it's")) == "'it''s'"
    # Ohne Default und mit berechnetem Default: nicht darstellbar.
    assert _default_literal(Column("g", Integer)) is None
    assert _default_literal(Column("h", Integer, default=lambda: 7)) is None
