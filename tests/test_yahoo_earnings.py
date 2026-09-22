"""Tests der Earnings-Ausweichquelle - ohne Netz."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.sources import yahoo_earnings
from app.sources.earnings import YAHOO


def frame(rows: list[tuple]) -> pd.DataFrame:
    """Baut eine Tabelle in der Form, die yfinance liefert."""
    index = pd.DatetimeIndex(
        [pd.Timestamp(r[0], tz="America/New_York") for r in rows], name="Earnings Date"
    )
    return pd.DataFrame(
        {
            "EPS Estimate": [r[1] for r in rows],
            "Reported EPS": [r[2] for r in rows],
            "Surprise(%)": [r[3] for r in rows],
        },
        index=index,
    )


@pytest.fixture
def fake_yf(monkeypatch):
    holder: dict[str, object] = {}

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def get_earnings_dates(self, limit=24):
            value = holder.get("frame")
            if isinstance(value, Exception):
                raise value
            return value

        @property
        def calendar(self):
            value = holder.get("calendar")
            if isinstance(value, Exception):
                raise value
            return value

    monkeypatch.setattr(yahoo_earnings.yf, "Ticker", FakeTicker)
    return holder


# -- Meldezeitpunkt ---------------------------------------------------------


def test_us_titel_nach_boersenschluss():
    stamp = dt.datetime(2026, 7, 30, 16, 0)
    assert yahoo_earnings.derive_hour("AAPL", stamp) == "amc"


def test_us_titel_vor_handelsbeginn():
    stamp = dt.datetime(2026, 7, 30, 7, 30)
    assert yahoo_earnings.derive_hour("AAPL", stamp) == "bmo"


def test_us_titel_waehrend_des_handels():
    stamp = dt.datetime(2026, 7, 30, 12, 0)
    assert yahoo_earnings.derive_hour("AAPL", stamp) == "dmh"


def test_europaeischer_titel_bleibt_unbekannt():
    """Yahoo rechnet in New Yorker Zeit um - daraus lässt sich die Frankfurter
    Meldezeit nicht rekonstruieren. Unbekannt heißt: Einstieg am Folgetag."""
    stamp = dt.datetime(2026, 8, 4, 20, 0)
    assert yahoo_earnings.derive_hour("SAP.DE", stamp) is None
    assert yahoo_earnings.derive_hour("BY6.F", stamp) is None


def test_us_erkennung():
    assert yahoo_earnings.is_us_symbol("AAPL") is True
    assert yahoo_earnings.is_us_symbol("SAP.DE") is False


# -- Abruf ------------------------------------------------------------------


def test_gemeldete_quartale_werden_uebernommen(fake_yf):
    fake_yf["frame"] = frame([
        ("2026-07-30 16:00", 1.89, 2.02, 6.74),
        ("2026-04-30 16:00", 1.94, 2.01, 3.46),
    ])
    rows = yahoo_earnings.fetch_earnings("AAPL")

    assert len(rows) == 2
    assert rows[0].report_date == dt.date(2026, 7, 30)   # neueste zuerst
    assert rows[0].eps_estimate == pytest.approx(1.89)
    assert rows[0].eps_actual == pytest.approx(2.02)
    assert rows[0].surprise_percent == pytest.approx(6.74)
    assert rows[0].hour == "amc"
    assert rows[0].source == YAHOO


def test_nicht_gemeldete_termine_fallen_raus(fake_yf):
    """Ein angekündigter Termin ohne Ergebnis ist kein Signal-Auslöser."""
    fake_yf["frame"] = frame([
        ("2026-10-29 16:00", 1.98, float("nan"), float("nan")),
        ("2026-07-30 16:00", 1.89, 2.02, 6.74),
    ])
    rows = yahoo_earnings.fetch_earnings("AAPL")

    assert len(rows) == 1
    assert rows[0].report_date == dt.date(2026, 7, 30)


def test_leere_tabelle(fake_yf):
    fake_yf["frame"] = pd.DataFrame()
    assert yahoo_earnings.fetch_earnings("AAPL") == []


def test_none_statt_tabelle(fake_yf):
    fake_yf["frame"] = None
    assert yahoo_earnings.fetch_earnings("AAPL") == []


def test_abruffehler_wird_als_eigene_ausnahme_gemeldet(fake_yf):
    fake_yf["frame"] = RuntimeError("Yahoo antwortet nicht")
    with pytest.raises(yahoo_earnings.YahooEarningsError) as exc:
        yahoo_earnings.fetch_earnings("AAPL")
    assert "AAPL" in str(exc.value)


def test_fehlende_schaetzung_bricht_nicht(fake_yf):
    fake_yf["frame"] = frame([("2026-07-30 16:00", float("nan"), 2.02, float("nan"))])
    rows = yahoo_earnings.fetch_earnings("AAPL")
    assert len(rows) == 1
    assert rows[0].eps_estimate is None
    assert rows[0].eps_actual == pytest.approx(2.02)


# -- Nächster Termin --------------------------------------------------------


def test_naechster_termin(fake_yf):
    fake_yf["calendar"] = {"Earnings Date": [dt.date(2026, 11, 11), dt.date(2027, 2, 10)]}
    heute = dt.date(2026, 9, 22)
    assert yahoo_earnings.fetch_next_earnings_date("ENR.DE", heute) == dt.date(2026, 11, 11)


def test_vergangene_termine_werden_uebergangen(fake_yf):
    """Yahoos Kalender enthält auch alte Termine. Ein vergangener Wert wäre
    nicht nur falsch angezeigt - er löste bei jedem Lauf einen erneuten
    Ausblick-Abruf aus, weil der gemerkte Termin verstrichen ist."""
    fake_yf["calendar"] = {
        "Earnings Date": [dt.date(2026, 7, 30), dt.date(2026, 8, 6), dt.date(2026, 11, 11)]
    }
    heute = dt.date(2026, 9, 22)
    assert yahoo_earnings.fetch_next_earnings_date("AFX.DE", heute) == dt.date(2026, 11, 11)


def test_nur_vergangene_termine_ergeben_keinen_naechsten(fake_yf):
    fake_yf["calendar"] = {"Earnings Date": [dt.date(2026, 7, 30)]}
    assert yahoo_earnings.fetch_next_earnings_date("AFX.DE", dt.date(2026, 9, 22)) is None


def test_termin_heute_zaehlt_noch(fake_yf):
    heute = dt.date(2026, 9, 22)
    fake_yf["calendar"] = {"Earnings Date": [heute]}
    assert yahoo_earnings.fetch_next_earnings_date("AFX.DE", heute) == heute


def test_einzelnes_datum_statt_liste(fake_yf):
    fake_yf["calendar"] = {"Earnings Date": dt.date(2026, 11, 11)}
    assert yahoo_earnings.fetch_next_earnings_date(
        "ENR.DE", dt.date(2026, 9, 22)
    ) == dt.date(2026, 11, 11)


def test_kein_termin_bekannt(fake_yf):
    fake_yf["calendar"] = {"Ex-Dividend Date": dt.date(2026, 4, 21)}
    assert yahoo_earnings.fetch_next_earnings_date("AIR.DE") is None


def test_unerwartete_kalenderform(fake_yf):
    fake_yf["calendar"] = "Unsinn"
    assert yahoo_earnings.fetch_next_earnings_date("AIR.DE") is None
