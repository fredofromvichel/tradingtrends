"""Engine, Session-Factory und Schema-Erstellung."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, Ticker

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine(db_path: Path) -> Engine:
    """Legt die Engine an und erstellt fehlende Tabellen."""
    global _engine, _SessionLocal

    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        # Der Scheduler-Thread und die Request-Threads teilen sich die Engine.
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - Infrastruktur
        cur = dbapi_conn.cursor()
        # WAL: paralleles Lesen waehrend ein Pipeline-Lauf schreibt.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    _engine = engine
    _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    log.info("SQLite bereit: %s", db_path)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Ergaenzt Spalten, die in den Modellen stehen, aber in der Datei fehlen.

    ``create_all`` legt fehlende Tabellen an, aber keine fehlenden Spalten. Ohne
    das hier muesste eine bestehende poc.db nach jeder Modellerweiterung
    geloescht werden - also samt aller bereits verfolgten Signale.

    Bewusst eng gehalten: nur neue, NULL-erlaubende Spalten werden angelegt.
    Geaenderte Typen, entfernte Spalten oder neue Constraints bleiben aussen vor
    und brauchen weiterhin ein 'make reset'.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # gerade frisch von create_all angelegt
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not column.nullable:
                log.warning(
                    "Spalte %s.%s fehlt, ist aber NOT NULL - automatisches "
                    "Ergaenzen waere unsicher. Bitte 'make reset' ausfuehren.",
                    table.name,
                    column.name,
                )
                continue
            ddl_type = column.type.compile(engine.dialect)
            with engine.begin() as conn:
                conn.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}')
                )
            log.info("Schema ergaenzt: %s.%s (%s)", table.name, column.name, ddl_type)


def get_sessionmaker() -> sessionmaker[Session]:
    if _SessionLocal is None:
        raise RuntimeError("init_engine() wurde noch nicht aufgerufen.")
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transaktionsklammer: commit bei Erfolg, rollback bei Fehler."""
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def sync_tickers(session: Session, symbols: tuple[str, ...]) -> None:
    """Gleicht die Ticker-Tabelle an config.yaml an.

    Symbole, die aus der Konfiguration verschwinden, werden auf active=False
    gesetzt statt geloescht - sonst wuerden Fremdschluessel bestehender
    Signale ins Leere zeigen.
    """
    existing = {t.symbol: t for t in session.scalars(select(Ticker)).all()}
    wanted = set(symbols)

    for symbol in symbols:
        row = existing.get(symbol)
        if row is None:
            session.add(Ticker(symbol=symbol, active=True))
            log.info("Neuer Ticker aufgenommen: %s", symbol)
        elif not row.active:
            row.active = True
            log.info("Ticker reaktiviert: %s", symbol)

    for symbol, row in existing.items():
        if symbol not in wanted and row.active:
            row.active = False
            log.info("Ticker deaktiviert (nicht mehr in config.yaml): %s", symbol)
