"""Momentum als getrennte Signalquelle im Pipeline-Lauf.

Kein Netzzugriff: Finnhub liefert nichts (die PEAD-Kette bleibt leer), die
Kurse kommen aus einer Attrappe mit vorgegebenen Verlaeufen.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select

import app.db as db_module
from app.config import Momentum, Schedule, Settings
from app.db import init_engine, session_scope
from app.models import MomentumRebalance, MomentumSignal, Price
from app.sources.prices import PriceRow

# Zwoelf Titel, damit ein Terzil drei Namen umfasst.
SYMBOLS = tuple(f"T{i:02d}" for i in range(1, 13))
LOOKBACK = 60
SKIP = 5
HOLDING = 5


def business_days(count: int, end: dt.date) -> list[dt.date]:
    days: list[dt.date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


@pytest.fixture
def trading_days() -> list[dt.date]:
    return business_days(140, dt.date.today())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        tickers=SYMBOLS,
        finnhub_api_key="test-key",
        db_path=tmp_path / "momentum.db",
        holding_period_days=20,
        price_backfill_days=300,
        price_refresh_days=7,
        lookback_days_earnings=3,
        schedule=Schedule(enabled=False),
        momentum=Momentum(
            enabled=True,
            lookback_days=LOOKBACK,
            skip_days=SKIP,
            group_fraction=0.25,      # 12 Titel -> je 3
            holding_period_days=HOLDING,
            min_universe=8,
        ),
    )


@pytest.fixture
def database(settings: Settings):
    init_engine(settings.db_path)
    yield
    db_module._engine = None
    db_module._SessionLocal = None


class SilentFinnhub:
    """Liefert nichts - so bleibt die PEAD-Kette aus dem Weg."""

    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_e):
        return None

    def company_profile(self, symbol):
        from app.sources.finnhub_client import CompanyProfile

        return CompanyProfile(symbol, f"{symbol} AG", "Software", "NASDAQ")

    def recommendations(self, symbol):
        return []

    def earnings_calendar(self, *_a, **_k):
        return []

    def earnings_surprises(self, *_a, **_k):
        return []


def price_source(trading_days, rates: dict[str, float]):
    """Jeder Titel waechst mit eigener Tagesrate - die Rangfolge ist damit bekannt.

    Multiplikativ, nicht linear: eine negative lineare Steigung wuerde ueber
    140 Tage in negative Kurse laufen.
    """

    def fetch(symbols, start, end):
        rows: list[PriceRow] = []
        for symbol in symbols:
            rate = rates.get(symbol, 0.0)
            for i, day in enumerate(trading_days):
                if not (start <= day <= end):
                    continue
                close = 100.0 * (1.0 + rate) ** i
                rows.append(PriceRow(symbol, day, close, close, close, close, 1000))
        return rows, []

    return fetch


@pytest.fixture
def patched(monkeypatch, trading_days):
    # T01 steigt am staerksten, T12 faellt am staerksten - Kurse bleiben positiv.
    rates = {
        symbol: (len(SYMBOLS) - i) * 0.0006 - 0.0025
        for i, symbol in enumerate(SYMBOLS)
    }
    monkeypatch.setattr("app.pipeline.FinnhubClient", SilentFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", price_source(trading_days, rates))
    return rates


# ---------------------------------------------------------------------------


def test_erster_lauf_bildet_einen_korb(settings, database, patched):
    from app.pipeline import run_daily

    result = run_daily(settings, trigger="test")
    assert result.status == "OK", result.errors
    assert result.momentum_opened == 6      # je 3 long und short

    with session_scope() as session:
        rebalance = session.scalar(select(MomentumRebalance))
        assert rebalance is not None
        assert rebalance.universe_size == 12
        assert rebalance.group_size == 3
        assert rebalance.period_key == f"{dt.date.today():%Y-%m}"

        longs = session.scalars(
            select(MomentumSignal).where(MomentumSignal.direction == "LONG")
        ).all()
        shorts = session.scalars(
            select(MomentumSignal).where(MomentumSignal.direction == "SHORT")
        ).all()
        assert {s.symbol for s in longs} == {"T01", "T02", "T03"}
        assert {s.symbol for s in shorts} == {"T10", "T11", "T12"}


def test_staerkster_titel_bekommt_rang_eins(settings, database, patched):
    from app.pipeline import run_daily

    run_daily(settings, trigger="test")
    with session_scope() as session:
        bester = session.scalar(
            select(MomentumSignal).where(MomentumSignal.rank == 1)
        )
        assert bester.symbol == "T01"
        assert bester.direction == "LONG"
        assert bester.momentum_score > 0


def test_fallende_titel_werden_short_mit_negativem_momentum(settings, database, patched):
    from app.pipeline import run_daily

    run_daily(settings, trigger="test")
    with session_scope() as session:
        schwaechster = session.scalar(
            select(MomentumSignal).order_by(MomentumSignal.rank.desc())
        )
        assert schwaechster.symbol == "T12"
        assert schwaechster.direction == "SHORT"
        assert schwaechster.momentum_score < 0


def test_zweiter_lauf_im_selben_monat_schichtet_nicht_um(settings, database, patched):
    """Der Periodenschluessel macht die Umschichtung idempotent."""
    from app.pipeline import run_daily

    run_daily(settings, trigger="test")
    result = run_daily(settings, trigger="test")

    assert result.momentum_opened == 0
    with session_scope() as session:
        assert len(session.scalars(select(MomentumRebalance)).all()) == 1
        assert len(session.scalars(select(MomentumSignal)).all()) == 6


def test_positionen_schliessen_nach_der_haltedauer(settings, database, patched, trading_days):
    from app.pipeline import run_daily
    from app.signals import CLOSED

    run_daily(settings, trigger="test")

    # Die Haltedauer verstreichen lassen: Einstieg um HOLDING Handelstage
    # zurueckdatieren.
    with session_scope() as session:
        for signal in session.scalars(select(MomentumSignal)).all():
            index = trading_days.index(signal.entry_date)
            signal.entry_date = trading_days[index - HOLDING - 1]
            price = session.get(
                Price, {"symbol": signal.symbol, "date": signal.entry_date}
            )
            signal.entry_price = price.close

    result = run_daily(settings, trigger="test")
    assert result.momentum_closed == 6

    with session_scope() as session:
        for signal in session.scalars(select(MomentumSignal)).all():
            assert signal.status == CLOSED
            assert signal.exit_date is not None
            assert signal.return_pct is not None


def test_short_rendite_ist_invertiert(settings, database, patched, trading_days):
    """Ein fallender Kurs muss auf der Short-Seite positiv zu Buche schlagen."""
    from app.pipeline import run_daily

    run_daily(settings, trigger="test")
    with session_scope() as session:
        for signal in session.scalars(select(MomentumSignal)).all():
            index = trading_days.index(signal.entry_date)
            signal.entry_date = trading_days[index - HOLDING - 1]
            price = session.get(Price, {"symbol": signal.symbol, "date": signal.entry_date})
            signal.entry_price = price.close

    run_daily(settings, trigger="test")

    with session_scope() as session:
        short = session.scalar(
            select(MomentumSignal).where(MomentumSignal.symbol == "T12")
        )
        assert short.direction == "SHORT"
        assert short.exit_price < short.entry_price   # Kurs gefallen
        assert short.return_pct > 0                   # Short gewinnt


def test_abgeschaltetes_momentum_legt_nichts_an(settings, database, monkeypatch, patched):
    import dataclasses

    from app.pipeline import run_daily

    aus = dataclasses.replace(
        settings, momentum=dataclasses.replace(settings.momentum, enabled=False)
    )
    result = run_daily(aus, trigger="test")

    assert result.momentum_opened == 0
    with session_scope() as session:
        assert session.scalar(select(MomentumRebalance)) is None


def test_zu_kurze_historie_meldet_sich_statt_still_zu_bleiben(
    settings, database, monkeypatch, trading_days
):
    """Ohne Meldung stuende auf der Seite nur eine leere Tabelle."""
    from app.pipeline import run_daily

    kurz = trading_days[-20:]     # weit weniger als LOOKBACK + 1
    rates = {symbol: 0.002 for symbol in SYMBOLS}
    monkeypatch.setattr("app.pipeline.FinnhubClient", SilentFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", price_source(kurz, rates))

    result = run_daily(settings, trigger="test")

    assert result.status == "PARTIAL"
    assert any("backfill" in e for e in result.errors)
    assert result.momentum_opened == 0


def test_pead_und_momentum_bleiben_getrennt(settings, database, patched):
    """Momentum darf keine Eintraege in der PEAD-Tabelle erzeugen."""
    from app.models import Signal
    from app.pipeline import run_daily

    run_daily(settings, trigger="test")

    with session_scope() as session:
        assert session.scalar(select(Signal)) is None          # PEAD leer
        assert len(session.scalars(select(MomentumSignal)).all()) == 6
