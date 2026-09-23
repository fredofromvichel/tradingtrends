"""Live gegen rueckwirkend: welche Signale in welche Statistik zaehlen."""
import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select

import app.db as db_module
from app.db import init_engine, session_scope
from app.models import RETRO_AFTER_DAYS, EarningsEvent, Signal, Ticker
from app.views import summary

HEUTE = dt.date(2026, 9, 23)


@pytest.fixture
def database(tmp_path: Path):
    init_engine(tmp_path / "herkunft.db")
    yield
    db_module._engine = None
    db_module._SessionLocal = None


def signal(session, tag: dt.date, angelegt: dt.date, rendite: float | None,
           benchmark: float | None = None):
    session.merge(Ticker(symbol="TEST", active=True))
    session.flush()
    event = EarningsEvent(symbol="TEST", report_date=tag, processed=True,
                          eps_actual=1.2, eps_estimate=1.0, surprise_pct=0.2)
    session.add(event)
    session.flush()
    s = Signal(
        symbol="TEST", earnings_event_id=event.id, signal_type="BUY",
        trigger_date=tag, entry_date=tag, entry_price=100.0, holding_period_days=20,
        status="CLOSED" if rendite is not None else "OPEN", return_pct=rendite,
        benchmark_return_pct=benchmark,
        created_at=dt.datetime.combine(angelegt, dt.time(22, 30)),
    )
    session.add(s)
    return s


def test_am_meldetag_angelegt_ist_live():
    s = Signal(trigger_date=HEUTE, created_at=dt.datetime.combine(HEUTE, dt.time(22, 30)))
    assert s.retro is False


def test_nach_einem_wochenende_noch_live():
    s = Signal(trigger_date=HEUTE,
               created_at=dt.datetime.combine(HEUTE + dt.timedelta(days=RETRO_AFTER_DAYS),
                                              dt.time(1, 0)))
    assert s.retro is False


def test_monate_spaeter_angelegt_ist_rueckwirkend():
    s = Signal(trigger_date=HEUTE - dt.timedelta(days=90),
               created_at=dt.datetime.combine(HEUTE, dt.time(12, 0)))
    assert s.retro is True


def test_sql_und_python_sagen_dasselbe(database):
    with session_scope() as session:
        signal(session, HEUTE - dt.timedelta(days=2), HEUTE, 0.05)
        signal(session, HEUTE - dt.timedelta(days=6), HEUTE, 0.05)
        signal(session, HEUTE - dt.timedelta(days=120), HEUTE, -0.02)
    with session_scope() as session:
        alle = session.scalars(select(Signal)).all()
        per_sql = set(session.scalars(select(Signal.id).where(Signal.retro)).all())
        assert per_sql == {s.id for s in alle if s.retro}
        assert len(per_sql) == 2


def test_statistik_trennt_nach_herkunft(database):
    with session_scope() as session:
        signal(session, HEUTE - dt.timedelta(days=40), HEUTE - dt.timedelta(days=39), 0.04, 0.01)
        signal(session, HEUTE - dt.timedelta(days=200), HEUTE, -0.03, 0.01)
        signal(session, HEUTE - dt.timedelta(days=150), HEUTE, 0.08, -0.01)
        signal(session, HEUTE - dt.timedelta(days=100), HEUTE, None)   # offen, rueckwirkend
    with session_scope() as session:
        live = summary(session)
        retro = summary(session, retro=True)
    assert (live["origin"], live["closed_count"], live["open_count"]) == ("live", 1, 0)
    assert (retro["origin"], retro["closed_count"], retro["open_count"]) == ("retro", 2, 1)
    assert retro["win_count"] == 1
    assert (retro["beat_count"], retro["beat_base"]) == (1, 2)
