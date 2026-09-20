"""SQLAlchemy-Modelle des POC."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Ticker(Base):
    __tablename__ = "tickers"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    # Ausgeschriebener Firmenname und Branche, einmalig von Finnhub geholt.
    name: Mapped[str | None] = mapped_column(String(128), default=None)
    industry: Mapped[str | None] = mapped_column(String(96), default=None)
    exchange: Mapped[str | None] = mapped_column(String(96), default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class EarningsEvent(Base):
    __tablename__ = "earnings_events"
    __table_args__ = (
        UniqueConstraint("symbol", "report_date", name="uq_earnings_symbol_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("tickers.symbol"), index=True, nullable=False)
    report_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    # Fiskalperiode laut Finnhub (z. B. "2026-06-30") - nur informativ.
    period: Mapped[str | None] = mapped_column(String(16), default=None)
    # Meldezeitpunkt laut Finnhub: bmo | amc | dmh | None.
    report_hour: Mapped[str | None] = mapped_column(String(8), default=None)
    eps_estimate: Mapped[float | None] = mapped_column(Float, default=None)
    eps_actual: Mapped[float | None] = mapped_column(Float, default=None)
    # Als Bruch gespeichert: 0.05 == +5 %.
    surprise_pct: Mapped[float | None] = mapped_column(Float, default=None)
    sue: Mapped[float | None] = mapped_column(Float, default=None)
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    # True, sobald die Signal-Entscheidung getroffen wurde (auch wenn sie
    # "kein Signal" lautete). Macht den Lauf idempotent.
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class Price(Base):
    __tablename__ = "prices"

    symbol: Mapped[str] = mapped_column(ForeignKey("tickers.symbol"), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(Float, default=None)
    high: Mapped[float | None] = mapped_column(Float, default=None)
    low: Mapped[float | None] = mapped_column(Float, default=None)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int | None] = mapped_column(Integer, default=None)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("earnings_event_id", name="uq_signal_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("tickers.symbol"), index=True, nullable=False)
    earnings_event_id: Mapped[int] = mapped_column(
        ForeignKey("earnings_events.id"), nullable=False
    )
    signal_type: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY | SELL
    trigger_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    entry_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    entry_price: Mapped[float | None] = mapped_column(Float, default=None)
    holding_period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    exit_price: Mapped[float | None] = mapped_column(Float, default=None)
    status: Mapped[str] = mapped_column(
        String(8), default="OPEN", nullable=False, index=True
    )  # OPEN | CLOSED
    return_pct: Mapped[float | None] = mapped_column(Float, default=None)
    # Kennzahlen zum Zeitpunkt der Entscheidung, damit die Anzeige nicht von
    # spaeteren Neuberechnungen abhaengt.
    sue_at_signal: Mapped[float | None] = mapped_column(Float, default=None)
    surprise_pct_at_signal: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class PipelineRun(Base):
    """Protokoll der Pipeline-Laeufe - Grundlage der Statusanzeige im Frontend."""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # scheduler | manual | startup
    status: Mapped[str] = mapped_column(String(16), default="RUNNING", nullable=False)
    events_ingested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prices_ingested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    signals_opened: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    signals_closed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(String(2000), default=None)
