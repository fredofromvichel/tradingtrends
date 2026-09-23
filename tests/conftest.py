"""Gemeinsame Vorgaben fuer alle Tests."""
import pytest


@pytest.fixture(autouse=True)
def handelstag_abgeschlossen(monkeypatch):
    """Die Tests rechnen wie der naechtliche Lauf: nach Boersenschluss.

    Ohne diese Vorgabe haengen sie von der Uhrzeit ab - vor 22:15 Uhr
    stellte die Pipeline die heutigen Kurse zurueck. Wer das Verhalten
    waehrend des Handels pruefen will, ueberschreibt es im Test.
    """
    monkeypatch.setattr("app.pipeline.trading_day_complete", lambda now=None: True)


@pytest.fixture(autouse=True)
def kein_yahoo_im_test(monkeypatch):
    """Kein echter Yahoo-Zugriff aus Versehen.

    Die Finnhub-Attrappen liefern keinen Meldetermin; die Pipeline fragt dann
    die Ausweichquelle - ohne diese Vorgabe wirklich im Netz, und ein Test
    haengt stillschweigend davon ab, was Yahoo gerade antwortet. Die Attrappe
    verhaelt sich wie Yahoo bei einem unbekannten Symbol: keine Daten, kein
    Fehler. Tests, die Yahoo pruefen, setzen ihre eigene darueber.
    """
    import yfinance

    class UnbekanntesSymbol:
        calendar: dict = {}

        def __init__(self, symbol, *_a, **_k):
            self.symbol = symbol

        def get_earnings_dates(self, *_a, **_k):
            return None

    monkeypatch.setattr(yfinance, "Ticker", UnbekanntesSymbol)
