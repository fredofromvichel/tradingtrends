"""Titelverwaltung ueber die Oberflaeche - mit echtem Routing, ohne Netz."""
import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.config import Momentum, Schedule, Settings
from app.db import session_scope
from app.models import Ticker
from app.symbols import Quote

BROWSER = {"origin": "http://testserver"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    settings = Settings(
        tickers=("NVDA", "AIR.DE"),
        finnhub_api_key="test",
        db_path=tmp_path / "ui.db",
        schedule=Schedule(enabled=False),
        momentum=Momentum(enabled=False),
    )
    import app.main as main

    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main, "configure_logging", lambda *_a, **_k: None)
    laeufe: list[str] = []
    monkeypatch.setattr(main, "request_background_run", lambda s, trigger="titel": laeufe.append(trigger) or True)

    with TestClient(main.app) as c:
        c.laeufe = laeufe
        yield c
    db_module._engine = None
    db_module._SessionLocal = None


def token(client) -> str:
    from app import csrf

    return csrf.token()


def aktiv():
    with session_scope() as session:
        return sorted(
            t.symbol for t in session.query(Ticker).filter(Ticker.active.is_(True))
        )


# -- Anzeige ------------------------------------------------------------------


def test_startliste_landet_in_der_liste(client):
    r = client.get("/titel")
    assert r.status_code == 200
    assert "NVDA" in r.text and "AIR.DE" in r.text
    assert 'name="q"' in r.text


def test_formulare_tragen_das_token(client):
    r = client.get("/")
    assert f'value="{token(client)}"' in r.text


def test_export_block_steht_auf_der_seite(client):
    r = client.get("/titel")
    assert "tickers:\n  - AIR.DE\n  - NVDA" in r.text


# -- Pruefung -----------------------------------------------------------------


@pytest.fixture
def yahoo(monkeypatch):
    suche = {
        "AOMD": [Quote("AOMD", "Angel Oak Mortgage REIT", "NYQ", "NYSE"),
                 Quote("AOMD.DE", "Alstom SA", "GER", "XETRA")],
        "Carl Zeiss Meditec": [Quote("AFX.DE", "Carl Zeiss Meditec AG", "GER", "XETRA")],
        "NVDA": [Quote("NVDA", "NVIDIA Corporation", "NMS", "NASDAQ")],
    }
    monkeypatch.setattr("app.symbols.search_quotes", lambda q: suche.get(q, []))
    monkeypatch.setattr(
        "app.symbols.probe_prices",
        lambda syms: {s: (dt.date(2026, 9, 23), 15.4) for s in syms},
    )


def test_mehrdeutige_eingabe_waehlt_nichts_vor(client, yahoo):
    r = client.get("/titel", params={"q": "AOMD"})
    assert "Angel Oak Mortgage REIT" in r.text and "Alstom SA" in r.text
    assert "Mehrdeutig" in r.text
    # Einzig "keinen davon" ist angehakt.
    assert r.text.count("checked") == 1
    assert 'value="" checked' in r.text


def test_eindeutige_eingabe_waehlt_das_empfohlene_listing(client, yahoo):
    r = client.get("/titel", params={"q": "Carl Zeiss Meditec"})
    assert 'value="AFX.DE"\n                       checked' in r.text


def test_bereits_beobachtetes_listing_ist_gesperrt(client, yahoo):
    r = client.get("/titel", params={"q": "NVDA"})
    assert "bereits beobachtet" in r.text
    assert 'value="" checked' in r.text


# -- Aufnehmen ----------------------------------------------------------------


def test_aufnehmen_legt_titel_an_und_startet_lauf(client):
    r = client.post(
        "/titel/aufnehmen",
        data={"csrf": token(client), "wahl-0": "AOMD.DE", "n:AOMD.DE": "Alstom SA"},
        headers=BROWSER, follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/titel?aufgenommen=AOMD.DE"
    assert "AOMD.DE" in aktiv()
    assert client.laeufe == ["titel"]
    with session_scope() as session:
        assert session.get(Ticker, "AOMD.DE").name == "Alstom SA"


def test_rueckmeldung_nach_dem_aufnehmen(client):
    r = client.get("/titel", params={"aufgenommen": "AOMD.DE"})
    assert "AOMD.DE aufgenommen" in r.text


def test_nichts_gewaehlt(client):
    r = client.post("/titel/aufnehmen", data={"csrf": token(client), "wahl-0": ""},
                    headers=BROWSER, follow_redirects=False)
    assert r.headers["location"] == "/titel?nichts=1"
    assert client.laeufe == []


def test_bereits_beobachteter_titel_startet_keinen_lauf(client):
    r = client.post("/titel/aufnehmen", data={"csrf": token(client), "wahl-0": "NVDA"},
                    headers=BROWSER, follow_redirects=False)
    assert r.headers["location"] == "/titel?bereits=NVDA"
    assert client.laeufe == []


def test_unzulaessiges_symbol_wird_ignoriert(client):
    r = client.post("/titel/aufnehmen",
                    data={"csrf": token(client), "wahl-0": "<script>"},
                    headers=BROWSER, follow_redirects=False)
    assert r.headers["location"] == "/titel?nichts=1"


# -- Entfernen und zurueck ------------------------------------------------------


def test_entfernen_und_rueckgaengig(client):
    r = client.post("/titel/NVDA/entfernen", data={"csrf": token(client)},
                    headers=BROWSER, follow_redirects=False)
    assert r.headers["location"] == "/titel?entfernt=NVDA"
    assert aktiv() == ["AIR.DE"]

    seite = client.get("/titel?entfernt=NVDA").text
    assert "NVDA wird nicht mehr beobachtet" in seite
    assert 'action="/titel/NVDA/aufnehmen"' in seite
    assert "Nicht mehr beobachtet (1)" in seite

    r = client.post("/titel/NVDA/aufnehmen", data={"csrf": token(client)},
                    headers=BROWSER, follow_redirects=False)
    assert r.headers["location"] == "/titel?reaktiviert=NVDA"
    assert aktiv() == ["AIR.DE", "NVDA"]


def test_steckbrief_eines_entfernten_titels(client):
    client.post("/titel/NVDA/entfernen", data={"csrf": token(client)}, headers=BROWSER)
    seite = client.get("/titel/NVDA").text
    assert "NVDA wird nicht mehr beobachtet" in seite
    assert "Nicht mehr beobachten</button>" not in seite

    r = client.post("/titel/NVDA/aufnehmen",
                    data={"csrf": token(client), "zurueck": "steckbrief"},
                    headers=BROWSER, follow_redirects=False)
    assert r.headers["location"] == "/titel/NVDA"


def test_entfernen_unbekannter_titel(client):
    r = client.post("/titel/GIBTSNICHT/entfernen", data={"csrf": token(client)},
                    headers=BROWSER)
    assert r.status_code == 404


# -- CSRF -----------------------------------------------------------------------


@pytest.mark.parametrize("pfad", ["/titel/aufnehmen", "/titel/NVDA/entfernen",
                                  "/titel/NVDA/aufnehmen", "/run-now"])
def test_ohne_token_passiert_nichts(client, pfad):
    r = client.post(pfad, data={"wahl-0": "EVIL"}, headers=BROWSER)
    assert r.status_code == 403
    assert "abgelaufen" in r.text
    assert aktiv() == ["AIR.DE", "NVDA"]


@pytest.mark.parametrize("pfad", ["/titel/aufnehmen", "/titel/NVDA/entfernen", "/run-now"])
def test_fremde_herkunft_wird_abgewiesen_trotz_token(client, pfad):
    r = client.post(pfad, data={"csrf": token(client), "wahl-0": "EVIL"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert "nicht von einer Seite dieser Anwendung" in r.text
    assert aktiv() == ["AIR.DE", "NVDA"]


def test_lesen_ist_von_fremder_herkunft_aus_erlaubt(client):
    """Links von anderen Seiten auf die Anwendung muessen weiter funktionieren."""
    r = client.get("/titel", headers={"origin": "https://example.org"})
    assert r.status_code == 200
