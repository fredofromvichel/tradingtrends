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
from sqlalchemy import func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# Ein Signal, das spaeter als so viele Kalendertage nach der Meldung angelegt
# wurde, haette man nicht rechtzeitig handeln koennen: es ist rueckwirkend
# berechnet (make seed, Nachladen fuer neu aufgenommene Titel). Fuenf Tage
# decken ein Wochenende und einen verpassten Nachtlauf ab.
RETRO_AFTER_DAYS = 5


class Ticker(Base):
    __tablename__ = "tickers"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    # Ausgeschriebener Firmenname und Branche, einmalig von Finnhub geholt.
    name: Mapped[str | None] = mapped_column(String(128), default=None)
    industry: Mapped[str | None] = mapped_column(String(96), default=None)
    exchange: Mapped[str | None] = mapped_column(String(96), default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Naechster erwarteter Meldetermin laut Finnhub-Kalender.
    next_earnings_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    outlook_fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    # Wie weit zurueck Kurse bzw. Earnings fuer diesen Titel schon geholt
    # wurden (Kalendertage). Ist eine Einstellung groesser als das Gemerkte,
    # holt der naechste Lauf die Tiefe einmal nach - fuer neue Titel wie fuer
    # alte nach einer Konfigurationsaenderung.
    price_history_days: Mapped[int | None] = mapped_column(Integer, default=None)
    earnings_history_days: Mapped[int | None] = mapped_column(Integer, default=None)


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
    # Welche Quelle das Event geliefert hat: finnhub | yahoo.
    source: Mapped[str | None] = mapped_column(String(16), default=None)
    eps_estimate: Mapped[float | None] = mapped_column(Float, default=None)
    eps_actual: Mapped[float | None] = mapped_column(Float, default=None)
    # Als Bruch gespeichert: 0.05 == +5 %.
    surprise_pct: Mapped[float | None] = mapped_column(Float, default=None)
    sue: Mapped[float | None] = mapped_column(Float, default=None)
    # Zutaten des SUE, damit der Rechenweg nachvollziehbar bleibt: durch
    # welche Streuung geteilt wurde und auf wie vielen Quartalen sie beruht.
    surprise_stdev: Mapped[float | None] = mapped_column(Float, default=None)
    history_count: Mapped[int | None] = mapped_column(Integer, default=None)
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
    # Was die uebrigen beobachteten Titel im selben Zeitraum gemacht haben.
    # Ohne diesen Massstab sagt eine Rendite fuer sich genommen wenig.
    benchmark_return_pct: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    @hybrid_property
    def retro(self) -> bool:
        """Rueckwirkend berechnet statt zum Zeitpunkt der Meldung erzeugt.

        Abgeleitet statt gespeichert: der Anlagezeitpunkt steht ohnehin in
        der Zeile, und so gilt die Regel auch fuer Signale aus der Zeit vor
        dieser Unterscheidung.
        """
        created = self.created_at.date() if self.created_at else dt.date.today()
        return (created - self.trigger_date).days > RETRO_AFTER_DAYS

    @retro.inplace.expression
    @classmethod
    def _retro_expression(cls):
        return (
            func.julianday(func.date(cls.created_at)) - func.julianday(cls.trigger_date)
            > RETRO_AFTER_DAYS
        )


class AnalystRecommendation(Base):
    """Analystenbild von Finnhub - Fremddaten, nicht Teil der PEAD-Logik."""

    __tablename__ = "analyst_recommendations"
    __table_args__ = (
        UniqueConstraint("symbol", "period", name="uq_reco_symbol_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("tickers.symbol"), index=True, nullable=False)
    # Monatsanfang, so liefert Finnhub die Zeitreihe.
    period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    strong_buy: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    buy: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sell: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    strong_sell: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class FinnhubCoverage(Base):
    """Gemerkte Tarif-Sperren je Symbol und Endpunkt.

    Ein 403 ist strukturell. Ohne dieses Gedaechtnis liefe jeder Tageslauf
    dieselben aussichtslosen Anfragen erneut - bei einem gemischten Universum
    schnell ein paar Dutzend pro Lauf.
    """

    __tablename__ = "finnhub_coverage"
    __table_args__ = (
        UniqueConstraint("symbol", "endpoint", name="uq_coverage_symbol_endpoint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    blocked_since: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    # Wann zuletzt erneut probiert wurde - steuert die Wiedervorlage.
    last_checked: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class MomentumRebalance(Base):
    """Eine Umschichtung des Momentum-Korbs."""

    __tablename__ = "momentum_rebalances"
    __table_args__ = (
        UniqueConstraint("period_key", name="uq_rebalance_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Periodenschluessel, z. B. "2026-09" - macht den Lauf idempotent.
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)
    rebalance_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    formation_start: Mapped[dt.date | None] = mapped_column(Date, default=None)
    formation_end: Mapped[dt.date | None] = mapped_column(Date, default=None)
    universe_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    group_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lookback_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skip_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class MomentumSignal(Base):
    """Eine Position aus dem Momentum-Korb.

    Bewusst eine eigene Tabelle neben `signals`: die beiden Quellen werden
    getrennt ausgewiesen, damit bei einem Ergebnis immer klar ist, welche
    Logik es getragen hat.
    """

    __tablename__ = "momentum_signals"
    __table_args__ = (
        UniqueConstraint("rebalance_id", "symbol", name="uq_momentum_rebalance_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(ForeignKey("tickers.symbol"), index=True, nullable=False)
    rebalance_id: Mapped[int] = mapped_column(
        ForeignKey("momentum_rebalances.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(5), nullable=False)  # LONG | SHORT
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    momentum_score: Mapped[float] = mapped_column(Float, nullable=False)
    entry_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    entry_price: Mapped[float | None] = mapped_column(Float, default=None)
    holding_period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_date: Mapped[dt.date | None] = mapped_column(Date, default=None)
    exit_price: Mapped[float | None] = mapped_column(Float, default=None)
    status: Mapped[str] = mapped_column(String(8), default="OPEN", nullable=False, index=True)
    return_pct: Mapped[float | None] = mapped_column(Float, default=None)
    benchmark_return_pct: Mapped[float | None] = mapped_column(Float, default=None)
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
    momentum_opened: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    momentum_closed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Abgerufene Kurszeilen, getrennt von den neu geschriebenen: ein
    # Wiederholungslauf schreibt 0 neue, obwohl der Abruf funktioniert hat.
    prices_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(String(2000), default=None)
    # Hinweise ohne Fehlercharakter - etwa Titel, die der Finnhub-Tarif
    # dauerhaft nicht abdeckt. Setzen den Lauf nicht auf PARTIAL.
    notes: Mapped[str | None] = mapped_column(String(2000), default=None)
