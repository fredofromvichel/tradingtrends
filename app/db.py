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


class SchemaOutOfDate(RuntimeError):
    """Die Datei laesst sich nicht gefahrlos auf den Modellstand bringen."""

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


def _default_literal(column) -> str | None:
    """SQL-Literal des Spalten-Defaults, oder None wenn nicht darstellbar.

    Nur feste Werte lassen sich in ein ALTER TABLE schreiben. Ein Default, der
    zur Laufzeit in Python berechnet wird (etwa ein Zeitstempel), kann SQLite
    fuer bestehende Zeilen nicht erzeugen.
    """
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _add_missing_columns(engine: Engine) -> None:
    """Ergaenzt Spalten, die in den Modellen stehen, aber in der Datei fehlen.

    ``create_all`` legt fehlende Tabellen an, aber keine fehlenden Spalten. Ohne
    das hier muesste eine bestehende poc.db nach jeder Modellerweiterung
    geloescht werden - also samt aller bereits verfolgten Signale.

    NULL-erlaubende Spalten kommen einfach dazu. Eine NOT-NULL-Spalte braucht
    einen festen Default, den SQLite fuer die bestehenden Zeilen einsetzen kann;
    fehlt der, bricht der Start mit einer lesbaren Meldung ab. Das ist der
    Unterschied zu einem stillen Ueberspringen: danach schlaegt jede Abfrage
    dieser Tabelle mit 'no such column' fehl, und die Ursache steht nirgends.

    Geaenderte Typen, entfernte Spalten und neue Constraints deckt das nicht ab -
    dafuer bleibt 'make reset'.
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

            ddl = f'"{column.name}" {column.type.compile(engine.dialect)}'
            if not column.nullable:
                literal = _default_literal(column)
                if literal is None:
                    raise SchemaOutOfDate(
                        f"Die Datenbank kennt die Spalte {table.name}.{column.name} "
                        "noch nicht. Sie ist NOT NULL und hat keinen festen "
                        "Standardwert, laesst sich also nicht nachtraeglich "
                        "einfuegen.\n"
                        "Abhilfe: 'make reset' loescht die Datenbank; Kurse holt "
                        "'make backfill' zurueck, Earnings 'make seed'."
                    )
                ddl += f" NOT NULL DEFAULT {literal}"

            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))
            log.info("Schema ergaenzt: %s.%s", table.name, column.name)


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
