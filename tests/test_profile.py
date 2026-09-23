"""Firmennamen und Branche: Finnhub, wo es darf, Yahoo fuer den Rest."""
from pathlib import Path

import pytest
from sqlalchemy import select

import app.db as db_module
from app.config import Momentum, Schedule, Settings
from app.db import init_engine, session_scope
from app.models import Ticker
from app.sources.finnhub_client import CompanyProfile, FinnhubForbiddenError
from app.sources.yahoo_profile import YahooProfile, parse


def test_parse_nimmt_das_exakte_symbol():
    quotes = [
        {"symbol": "AFX.F", "longname": "Falsch"},
        {"symbol": "AFX.DE", "longname": "Carl Zeiss Meditec AG",
         "shortname": "Carl Zeiss Meditec AG         I", "sectorDisp": "Healthcare",
         "industryDisp": "Medical Instruments & Supplies", "exchDisp": "XETRA"},
    ]
    p = parse("afx.de", quotes)
    assert p == YahooProfile("AFX.DE", "Carl Zeiss Meditec AG", "Healthcare",
                             "Medical Instruments & Supplies", "XETRA")


def test_parse_kurzname_ohne_fuellzeichen():
    p = parse("X.DE", [{"symbol": "X.DE", "shortname": "Alstom S.A.        A"}])
    assert p.name == "Alstom S.A. A"


def test_parse_ohne_treffer():
    assert parse("X.DE", [{"symbol": "Y.DE", "longname": "Y"}]) is None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(tickers=(), finnhub_api_key="k", db_path=tmp_path / "p.db",
                    schedule=Schedule(enabled=False), momentum=Momentum(enabled=False))


@pytest.fixture
def db(settings):
    init_engine(settings.db_path)
    yield
    db_module._engine = None
    db_module._SessionLocal = None


class Finnhub:
    calls: list = []

    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def company_profile(self, symbol):
        Finnhub.calls.append(symbol)
        if symbol == "BLOCK":
            raise FinnhubForbiddenError("403", endpoint="/stock/profile2")
        return CompanyProfile(symbol, f"{symbol} Inc", None, "NASDAQ")


@pytest.fixture
def quellen(monkeypatch):
    Finnhub.calls = []
    yahoo_calls: list = []

    def yahoo(symbol):
        yahoo_calls.append(symbol)
        return YahooProfile(symbol, f"{symbol} AG (Yahoo)", "Industrials",
                            "Aerospace & Defense", "XETRA")

    monkeypatch.setattr("app.pipeline.FinnhubClient", Finnhub)
    monkeypatch.setattr("app.sources.yahoo_profile.fetch_profile", yahoo)
    return yahoo_calls


def lauf(settings):
    from app.pipeline import _ingest_profiles
    _ingest_profiles(settings)


def zeile(symbol):
    with session_scope() as session:
        t = session.get(Ticker, symbol)
        return (t.name, t.industry, t.sector, t.exchange)


def test_deutscher_titel_bekommt_namen_und_branche_von_yahoo(settings, db, quellen):
    with session_scope() as session:
        session.add(Ticker(symbol="R3NK.DE", active=True))
    lauf(settings)
    assert zeile("R3NK.DE") == ("R3NK.DE AG (Yahoo)", "Aerospace & Defense", "Industrials", "XETRA")
    assert Finnhub.calls == []          # Finnhub darf das nicht - gar nicht erst fragen


def test_us_titel_finnhub_zuerst_yahoo_ergaenzt_branche(settings, db, quellen):
    with session_scope() as session:
        session.add(Ticker(symbol="NVDA", active=True))
    lauf(settings)
    name, industry, sector, exchange = zeile("NVDA")
    assert name == "NVDA Inc" and exchange == "NASDAQ"
    assert industry == "Aerospace & Defense" and sector == "Industrials"


def test_von_hand_eingetragener_name_bleibt(settings, db, quellen):
    with session_scope() as session:
        session.add(Ticker(symbol="AFX.DE", active=True, name="Mein Name", name_manual=True))
    lauf(settings)
    assert zeile("AFX.DE")[0] == "Mein Name"
    assert zeile("AFX.DE")[1] == "Aerospace & Defense"


def test_nicht_bei_jedem_lauf_erneut_fragen(settings, db, monkeypatch):
    aufrufe: list = []
    monkeypatch.setattr("app.pipeline.FinnhubClient", Finnhub)
    monkeypatch.setattr("app.sources.yahoo_profile.fetch_profile",
                        lambda s: aufrufe.append(s) or None)
    with session_scope() as session:
        session.add(Ticker(symbol="UNBEKANNT.DE", active=True))
    lauf(settings)
    lauf(settings)
    assert aufrufe == ["UNBEKANNT.DE"]


def test_netzfehler_wird_wiederholt(settings, db, monkeypatch):
    from app.sources.yahoo_profile import YahooProfileError

    def kaputt(symbol):
        raise YahooProfileError("Timeout")

    monkeypatch.setattr("app.pipeline.FinnhubClient", Finnhub)
    monkeypatch.setattr("app.sources.yahoo_profile.fetch_profile", kaputt)
    with session_scope() as session:
        session.add(Ticker(symbol="AIR.DE", active=True))
    with pytest.raises(RuntimeError):
        lauf(settings)
    with session_scope() as session:
        assert session.get(Ticker, "AIR.DE").profile_checked_at is None


def test_vollstaendige_titel_werden_nicht_gefragt(settings, db, quellen):
    with session_scope() as session:
        session.add(Ticker(symbol="SAP", active=True, name="SAP SE", industry="Software"))
    lauf(settings)
    assert quellen == [] and Finnhub.calls == []
