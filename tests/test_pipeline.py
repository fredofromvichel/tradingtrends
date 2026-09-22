"""End-to-end-Test der Pipeline mit ersetzten Datenquellen.

Kein Netzzugriff: Finnhub und yfinance werden durch Attrappen ersetzt,
die Datenbank ist eine temporaere SQLite-Datei.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select

import app.db as db_module
from app.config import Momentum, Schedule, Settings
from app.db import init_engine, session_scope
from app.models import EarningsEvent, Price, Signal
from app.sources.finnhub_client import CompanyProfile, RawEarnings
from app.sources.prices import PriceRow

SYMBOL = "TEST"


class FakeFinnhubBase:
    """Gemeinsames Geruest der Finnhub-Attrappen.

    Liefert Kontextmanager-Protokoll und ein Stammdatenprofil; die Testfaelle
    ueberschreiben nur, was sie wirklich variieren.
    """

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def company_profile(self, symbol):
        return CompanyProfile(symbol, f"{symbol} Testwerte AG", "Software", "NASDAQ")

    def recommendations(self, symbol):
        return []

    def earnings_calendar(self, symbol, date_from, date_to):
        return []

    def earnings_surprises(self, symbol):
        return []


def business_days(count: int, end: dt.date) -> list[dt.date]:
    """Die letzten ``count`` Werktage bis einschliesslich ``end``."""
    days: list[dt.date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        tickers=(SYMBOL,),
        finnhub_api_key="test-key",
        db_path=tmp_path / "test.db",
        holding_period_days=3,
        # Das Testereignis liegt 10 Handelstage zurueck - das Abruffenster
        # muss weit genug sein, um es zu erfassen.
        lookback_days_earnings=30,
        price_backfill_days=120,
        price_refresh_days=7,
        min_history_for_sue=4,
        schedule=Schedule(enabled=False),
        # Diese Datei prueft die PEAD-Kette. Momentum braucht ein Universum
        # von mindestens acht Titeln und wird in tests/test_momentum_pipeline.py
        # gesondert geprueft.
        momentum=Momentum(enabled=False),
    )


@pytest.fixture
def database(settings: Settings):
    init_engine(settings.db_path)
    yield
    db_module._engine = None
    db_module._SessionLocal = None


@pytest.fixture
def trading_days() -> list[dt.date]:
    return business_days(60, dt.date.today())


@pytest.fixture
def report_date(trading_days: list[dt.date]) -> dt.date:
    # Meldung vor 10 Handelstagen - genug Kurshistorie fuer Ein- und Ausstieg.
    return trading_days[-11]


@pytest.fixture
def patched_sources(monkeypatch, trading_days, report_date):
    """Finnhub und yfinance durch deterministische Attrappen ersetzen."""

    class FakeFinnhub(FakeFinnhubBase):

        def earnings_calendar(self, symbol, date_from, date_to):
            if report_date < date_from or report_date > date_to:
                return []
            return [
                RawEarnings(
                    symbol=symbol,
                    report_date=report_date,
                    eps_estimate=1.00,
                    eps_actual=1.20,  # +20 % Ueberraschung
                    surprise_percent=20.0,
                    period="2026-06-30",
                    hour="amc",  # nach Boersenschluss gemeldet
                )
            ]

        def earnings_surprises(self, symbol):
            # Vier ruhige Vorquartale -> kleine Streuung -> hoher SUE.
            base = report_date - dt.timedelta(days=365)
            out = []
            for i, surprise in enumerate([0.01, -0.01, 0.02, 0.00]):
                out.append(
                    RawEarnings(
                        symbol=symbol,
                        report_date=base + dt.timedelta(days=90 * i),
                        eps_estimate=1.00,
                        eps_actual=1.00 + surprise,
                        surprise_percent=surprise * 100,
                        period="hist",
                    )
                )
            return out

    def fake_fetch_prices(symbols, start, end):
        rows = [
            PriceRow(
                symbol=symbol,
                date=day,
                open=100.0,
                high=101.0,
                low=99.0,
                # Gleichmaessig steigend: Tag i kostet 100 + i.
                close=100.0 + index,
                volume=1_000_000,
            )
            for symbol in symbols
            for index, day in enumerate(trading_days)
            if start <= day <= end
        ]
        return rows, []

    monkeypatch.setattr("app.pipeline.FinnhubClient", FakeFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)


# ---------------------------------------------------------------------------


def test_erster_lauf_legt_event_kurse_und_signal_an(
    settings, database, patched_sources, report_date, trading_days
):
    from app.pipeline import run_daily

    result = run_daily(settings, trigger="test")

    assert result.status == "OK", result.errors
    assert result.events_ingested == 1
    assert result.signals_opened == 1
    assert result.prices_ingested > 0

    with session_scope() as session:
        event = session.scalar(select(EarningsEvent))
        assert event.symbol == SYMBOL
        assert event.report_date == report_date
        assert event.surprise_pct == pytest.approx(0.20)
        assert event.sue is not None and event.sue > 1.0
        assert event.processed is True

        signal = session.scalar(select(Signal))
        assert signal.signal_type == "BUY"
        assert signal.trigger_date == report_date
        # "amc": Einstieg erst am Folgetag, nicht am Schlusskurs des Meldetages.
        assert signal.entry_date == trading_days[trading_days.index(report_date) + 1]
        assert signal.entry_price is not None
        assert signal.holding_period_days == 3


def test_laengst_abgelaufene_position_schliesst_im_selben_lauf(
    settings, database, patched_sources
):
    """Beim Nachladen alter Earnings ist die Haltedauer schon vorbei.

    Die Signal-Erzeugung laeuft vor der Positionspflege, damit ein solches
    Signal nicht erst beim naechsten Lauf geschlossen wird.
    """
    from app.pipeline import run_daily

    result = run_daily(settings, trigger="seed")

    assert result.signals_opened == 1
    assert result.signals_closed == 1

    with session_scope() as session:
        signal = session.scalar(select(Signal))
        assert signal.status == "CLOSED"
        assert signal.exit_date is not None
        # Kurs steigt taeglich um 1, Haltedauer 3 Handelstage.
        assert signal.exit_price == pytest.approx(signal.entry_price + 3)
        assert signal.return_pct == pytest.approx(3 / signal.entry_price)


def test_frisches_signal_bleibt_offen(settings, database, monkeypatch, trading_days):
    """Ist die Haltedauer noch nicht um, bleibt die Position OPEN."""
    from app.pipeline import run_daily

    # Meldung am vorletzten Handelstag, vor Handelsbeginn -> Einstieg am
    # vorletzten Tag, danach nur noch ein Handelstag. Haltedauer 3.
    recent = trading_days[-2]

    class RecentFinnhub(FakeFinnhubBase):

        def earnings_calendar(self, symbol, date_from, date_to):
            return [RawEarnings(symbol, recent, 1.00, 1.20, 20.0, "q", "bmo")]

        def earnings_surprises(self, symbol):
            base = recent - dt.timedelta(days=365)
            return [
                RawEarnings(symbol, base + dt.timedelta(days=90 * i), 1.00,
                            1.00 + s, s * 100, "hist", None)
                for i, s in enumerate([0.01, -0.01, 0.02, 0.00])
            ]

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", RecentFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    result = run_daily(settings, trigger="test")

    assert result.signals_opened == 1
    assert result.signals_closed == 0

    with session_scope() as session:
        signal = session.scalar(select(Signal))
        assert signal.status == "OPEN"
        assert signal.entry_date == recent
        assert signal.exit_date is None
        assert signal.return_pct is None


def test_zweiter_lauf_aendert_nichts_mehr(settings, database, patched_sources):
    from app.pipeline import run_daily

    run_daily(settings, trigger="test")
    result = run_daily(settings, trigger="test")

    # Kein doppeltes Event, kein doppeltes Signal, kein zweites Schliessen.
    assert result.events_ingested == 0
    assert result.signals_opened == 0
    assert result.signals_closed == 0

    with session_scope() as session:
        assert len(session.scalars(select(Signal)).all()) == 1
        assert session.scalar(select(Signal)).status == "CLOSED"


def test_lauf_ist_idempotent(settings, database, patched_sources):
    from app.pipeline import run_daily

    for _ in range(3):
        run_daily(settings, trigger="test")

    with session_scope() as session:
        assert len(session.scalars(select(EarningsEvent)).all()) == 1
        assert len(session.scalars(select(Signal)).all()) == 1
        # Kurszeilen duerfen nicht dupliziert werden (PK auf symbol+date).
        prices = session.scalars(select(Price)).all()
        assert len(prices) == len({(p.symbol, p.date) for p in prices})


def test_kein_signal_unterhalb_der_schwelle(settings, database, monkeypatch, trading_days,
                                            report_date):
    """Kleine Ueberraschung bei hoher historischer Streuung -> kein Signal."""
    from app.pipeline import run_daily

    class QuietFinnhub(FakeFinnhubBase):

        def earnings_calendar(self, symbol, date_from, date_to):
            return [
                RawEarnings(symbol, report_date, 1.00, 1.01, 1.0, "2026-06-30")
            ]

        def earnings_surprises(self, symbol):
            base = report_date - dt.timedelta(days=365)
            return [
                RawEarnings(symbol, base + dt.timedelta(days=90 * i), 1.00,
                            1.00 + s, s * 100, "hist")
                for i, s in enumerate([0.10, -0.12, 0.15, -0.08])
            ]

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(s, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for s in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", QuietFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    result = run_daily(settings, trigger="test")

    assert result.events_ingested == 1
    assert result.signals_opened == 0
    with session_scope() as session:
        event = session.scalar(select(EarningsEvent))
        assert event.processed is True  # bewertet, aber verworfen
        assert session.scalar(select(Signal)) is None


def test_ausfall_einer_quelle_stoppt_den_lauf_nicht(settings, database, monkeypatch,
                                                    trading_days):
    """Faellt Finnhub aus, muessen Kurse trotzdem aktualisiert werden."""
    from app.pipeline import run_daily

    class BrokenFinnhub(FakeFinnhubBase):

        def earnings_calendar(self, *_a, **_k):
            raise RuntimeError("Finnhub nicht erreichbar")

        def earnings_surprises(self, *_a, **_k):
            raise RuntimeError("Finnhub nicht erreichbar")

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(s, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for s in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", BrokenFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    result = run_daily(settings, trigger="test")

    assert result.status == "PARTIAL"
    assert result.errors
    assert result.prices_ingested > 0


def test_force_price_backfill_zieht_das_grosse_fenster(settings, database, monkeypatch,
                                                       trading_days):
    """Ohne Zwang holt ein Folgelauf nur das kurze Aktualisierungsfenster.

    Beim Nachladen alter Earnings reicht das nicht - sonst fehlen die Kurse
    rund um das Ereignis und die Position bekaeme weder Ein- noch Ausstieg.
    """
    from app.pipeline import run_daily

    calls: list[dt.date] = []

    class NoFinnhub(FakeFinnhubBase):

        def earnings_calendar(self, *_a, **_k):
            return []

        def earnings_surprises(self, *_a, **_k):
            return []

    def recording_fetch_prices(symbols, start, end):
        calls.append(start)
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", NoFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", recording_fetch_prices)

    today = dt.date.today()
    run_daily(settings, trigger="test")                       # 1. Lauf: Backfill
    run_daily(settings, trigger="test")                       # 2. Lauf: kurzes Fenster
    run_daily(settings, trigger="seed", force_price_backfill=True)  # 3. Lauf: erzwungen

    assert calls[0] == today - dt.timedelta(days=settings.price_backfill_days)
    assert calls[1] == today - dt.timedelta(days=settings.price_refresh_days)
    assert calls[2] == today - dt.timedelta(days=settings.price_backfill_days)


def test_stammdaten_werden_nur_einmal_geholt(settings, database, monkeypatch, trading_days):
    """Ist der Firmenname bekannt, darf kein weiterer Profil-Request erfolgen."""
    from app.pipeline import run_daily
    from app.models import Ticker

    profile_calls: list[str] = []

    class CountingFinnhub(FakeFinnhubBase):
        def company_profile(self, symbol):
            profile_calls.append(symbol)
            return CompanyProfile(symbol, "Testwerte AG", "Software", "NASDAQ")

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", CountingFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    run_daily(settings, trigger="test")
    assert profile_calls == [SYMBOL]

    with session_scope() as session:
        ticker = session.get(Ticker, SYMBOL)
        assert ticker.name == "Testwerte AG"
        assert ticker.industry == "Software"
        assert ticker.exchange == "NASDAQ"

    run_daily(settings, trigger="test")
    assert profile_calls == [SYMBOL]  # kein zweiter Abruf


def test_fehlende_stammdaten_stoppen_den_lauf_nicht(settings, database, monkeypatch,
                                                    trading_days, report_date):
    """Ohne Firmenprofil muss die Signalerzeugung trotzdem laufen."""
    from app.pipeline import run_daily

    class NoProfileFinnhub(FakeFinnhubBase):
        def company_profile(self, symbol):
            raise RuntimeError("Profil nicht verfuegbar")

        def earnings_calendar(self, symbol, date_from, date_to):
            if report_date < date_from or report_date > date_to:
                return []
            return [RawEarnings(symbol, report_date, 1.00, 1.20, 20.0, "q", "amc")]

        def earnings_surprises(self, symbol):
            base = report_date - dt.timedelta(days=365)
            return [
                RawEarnings(symbol, base + dt.timedelta(days=90 * i), 1.00,
                            1.00 + s, s * 100, "hist", None)
                for i, s in enumerate([0.01, -0.01, 0.02, 0.00])
            ]

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", NoProfileFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    result = run_daily(settings, trigger="test")

    assert result.status == "PARTIAL"          # Stammdaten fehlgeschlagen
    assert result.signals_opened == 1          # Kerngeschaeft lief trotzdem


def test_ausblick_speichert_termin_und_empfehlungen(settings, database, monkeypatch,
                                                    trading_days):
    """Nächster Meldetermin und Analystenbild landen in der Datenbank."""
    from app.pipeline import run_daily
    from app.models import AnalystRecommendation, Ticker
    from app.sources.finnhub_client import Recommendation
    from sqlalchemy import select as sa_select

    naechster_termin = dt.date.today() + dt.timedelta(days=45)

    class OutlookFinnhub(FakeFinnhubBase):
        def earnings_calendar(self, symbol, date_from, date_to):
            # Nur der Vorausblick liefert etwas, das Rückblickfenster bleibt leer.
            # Beide Aufrufe enden in der Zukunft - allein date_from trennt sie.
            if date_from >= dt.date.today():
                return [RawEarnings(symbol, naechster_termin, 1.0, None, None, "q", "amc")]
            return []

        def recommendations(self, symbol):
            heute = dt.date.today().replace(day=1)
            return [
                Recommendation(symbol, heute, 12, 8, 5, 1, 0),
                Recommendation(symbol, heute - dt.timedelta(days=31), 10, 9, 6, 2, 0),
            ]

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", OutlookFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    result = run_daily(settings, trigger="test")
    assert result.status == "OK", result.errors

    with session_scope() as session:
        ticker = session.get(Ticker, SYMBOL)
        assert ticker.next_earnings_date == naechster_termin
        assert ticker.outlook_fetched_at is not None

        recos = session.scalars(sa_select(AnalystRecommendation)).all()
        assert len(recos) == 2
        neueste = max(recos, key=lambda r: r.period)
        assert neueste.strong_buy == 12
        assert neueste.hold == 5


def test_ausblick_wird_nicht_bei_jedem_lauf_neu_geholt(settings, database, monkeypatch,
                                                       trading_days):
    """Termin und Empfehlungen ändern sich langsam - sonst 2 Requests je Ticker und Tag."""
    from app.pipeline import run_daily

    calls: list[str] = []
    naechster_termin = dt.date.today() + dt.timedelta(days=45)

    class CountingFinnhub(FakeFinnhubBase):
        def earnings_calendar(self, symbol, date_from, date_to):
            if date_from >= dt.date.today():
                calls.append(f"kalender:{symbol}")
                return [RawEarnings(symbol, naechster_termin, 1.0, None, None, "q", "amc")]
            return []

        def recommendations(self, symbol):
            calls.append(f"reco:{symbol}")
            return []

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", CountingFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    run_daily(settings, trigger="test")
    nach_erstem = len(calls)
    assert nach_erstem == 2

    run_daily(settings, trigger="test")
    assert len(calls) == nach_erstem  # zweiter Lauf fragt nicht erneut


def test_verstrichener_termin_loest_neuen_abruf_aus(settings, database, monkeypatch,
                                                    trading_days):
    """Liegt der gemerkte Termin in der Vergangenheit, muss neu geholt werden."""
    from app.pipeline import run_daily
    from app.models import Ticker

    calls: list[str] = []

    class CountingFinnhub(FakeFinnhubBase):
        def earnings_calendar(self, symbol, date_from, date_to):
            if date_from >= dt.date.today():
                calls.append(symbol)
            return []

        def recommendations(self, symbol):
            return []

    def fake_fetch_prices(symbols, start, end):
        return [], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", CountingFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    run_daily(settings, trigger="test")
    assert len(calls) == 1

    with session_scope() as session:
        ticker = session.get(Ticker, SYMBOL)
        ticker.next_earnings_date = dt.date.today() - dt.timedelta(days=1)

    run_daily(settings, trigger="test")
    assert len(calls) == 2


def _forbidden(endpoint: str):
    from app.sources.finnhub_client import FinnhubForbiddenError

    return FinnhubForbiddenError(f"403 auf {endpoint}", endpoint=endpoint)


def test_tarifsperre_setzt_den_lauf_nicht_auf_partial(settings, database, monkeypatch,
                                                      trading_days):
    """Ein 403 ist strukturell, kein Fehler. Sonst hieße PARTIAL dauerhaft
    'alles normal' und echte Fehler gingen darin unter."""
    from app.pipeline import run_daily

    class ForbiddenFinnhub(FakeFinnhubBase):
        def company_profile(self, symbol):
            raise _forbidden("/stock/profile2")

        def earnings_calendar(self, *_a, **_k):
            raise _forbidden("/calendar/earnings")

        def earnings_surprises(self, *_a, **_k):
            raise _forbidden("/stock/earnings")

        def recommendations(self, *_a, **_k):
            raise _forbidden("/stock/recommendation")

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", ForbiddenFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    result = run_daily(settings, trigger="test")

    assert result.status == "OK", result.errors
    assert result.errors == []
    assert result.notes and "Finnhub-Tarif" in result.notes[0]
    assert SYMBOL in result.notes[0]
    # Der Kursteil läuft unberührt weiter.
    assert result.prices_ingested > 0


def test_gesperrte_endpunkte_werden_beim_zweiten_lauf_uebersprungen(
    settings, database, monkeypatch, trading_days
):
    """Ohne Gedächtnis liefe jeder Tageslauf dieselben aussichtslosen Anfragen."""
    from app.pipeline import run_daily

    calls: list[str] = []

    class CountingForbidden(FakeFinnhubBase):
        def company_profile(self, symbol):
            calls.append("profile")
            raise _forbidden("/stock/profile2")

        def earnings_calendar(self, *_a, **_k):
            calls.append("calendar")
            raise _forbidden("/calendar/earnings")

        def earnings_surprises(self, *_a, **_k):
            calls.append("earnings")
            raise _forbidden("/stock/earnings")

        def recommendations(self, *_a, **_k):
            calls.append("reco")
            raise _forbidden("/stock/recommendation")

    def fake_fetch_prices(symbols, start, end):
        return [], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", CountingForbidden)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)

    run_daily(settings, trigger="test")
    nach_erstem = len(calls)
    assert nach_erstem > 0

    run_daily(settings, trigger="test")
    assert len(calls) == nach_erstem, f"Zweiter Lauf hat erneut angefragt: {calls[nach_erstem:]}"


def test_freigeschalteter_endpunkt_hebt_die_sperre_auf(settings, database, monkeypatch,
                                                       trading_days, report_date):
    """Nach einem Tarif-Upgrade muss der Titel von selbst wieder mitlaufen."""
    from app.coverage import blocked_symbols
    from app.pipeline import run_daily

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    class ForbiddenFinnhub(FakeFinnhubBase):
        def company_profile(self, symbol):
            raise _forbidden("/stock/profile2")

        def earnings_calendar(self, *_a, **_k):
            raise _forbidden("/calendar/earnings")

        def earnings_surprises(self, *_a, **_k):
            raise _forbidden("/stock/earnings")

        def recommendations(self, *_a, **_k):
            raise _forbidden("/stock/recommendation")

    monkeypatch.setattr("app.pipeline.FinnhubClient", ForbiddenFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)
    run_daily(settings, trigger="test")

    with session_scope() as session:
        assert blocked_symbols(session) == [SYMBOL]

    # Sperren altern lassen, damit die Wiedervorlage greift. Der
    # Ausblick-Schritt laeuft ausserdem nur alle outlook_max_age_days - ohne
    # dessen Zeitstempel bliebe die Empfehlungs-Sperre unberuehrt.
    with session_scope() as session:
        from app.models import FinnhubCoverage, Ticker

        alt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
        for row in session.scalars(select(FinnhubCoverage)).all():
            row.last_checked = alt
        for ticker in session.scalars(select(Ticker)).all():
            ticker.outlook_fetched_at = alt

    # Jetzt liefert Finnhub wieder.
    class WorkingFinnhub(FakeFinnhubBase):
        def earnings_calendar(self, symbol, date_from, date_to):
            if date_from >= dt.date.today():
                return []
            return [RawEarnings(symbol, report_date, 1.00, 1.20, 20.0, "q", "amc")]

        def earnings_surprises(self, symbol):
            base = report_date - dt.timedelta(days=365)
            return [
                RawEarnings(symbol, base + dt.timedelta(days=90 * i), 1.00,
                            1.00 + s, s * 100, "hist", None)
                for i, s in enumerate([0.01, -0.01, 0.02, 0.00])
            ]

    monkeypatch.setattr("app.pipeline.FinnhubClient", WorkingFinnhub)
    result = run_daily(settings, trigger="test")

    with session_scope() as session:
        assert blocked_symbols(session) == []
    assert result.notes == []
    assert result.events_ingested == 1


def test_gesperrter_titel_bekommt_earnings_von_der_ausweichquelle(
    settings, database, monkeypatch, trading_days, report_date
):
    """Der eigentliche Zweck der Kette: 403 bei Finnhub darf den Titel nicht
    dauerhaft ohne PEAD-Signale lassen."""
    from app.models import EarningsEvent
    from app.pipeline import run_daily
    from app.sources.earnings import YAHOO, RawEarnings as SharedRaw

    class ForbiddenFinnhub(FakeFinnhubBase):
        def company_profile(self, symbol):
            raise _forbidden("/stock/profile2")

        def earnings_calendar(self, *_a, **_k):
            raise _forbidden("/calendar/earnings")

        def earnings_surprises(self, *_a, **_k):
            raise _forbidden("/stock/earnings")

        def recommendations(self, *_a, **_k):
            raise _forbidden("/stock/recommendation")

    def fake_yahoo(symbol, limit=24):
        basis = report_date - dt.timedelta(days=365)
        historie = [
            SharedRaw(symbol, basis + dt.timedelta(days=90 * i), 1.00, 1.00 + s,
                      s * 100, "hist", None, YAHOO)
            for i, s in enumerate([0.01, -0.01, 0.02, 0.00])
        ]
        return [SharedRaw(symbol, report_date, 1.00, 1.20, 20.0,
                          report_date.isoformat(), None, YAHOO)] + historie

    def fake_fetch_prices(symbols, start, end):
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []

    monkeypatch.setattr("app.pipeline.FinnhubClient", ForbiddenFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", fake_fetch_prices)
    monkeypatch.setattr("app.pipeline.yahoo_earnings.fetch_earnings", fake_yahoo)
    monkeypatch.setattr(
        "app.pipeline.yahoo_earnings.fetch_next_earnings_date", lambda s: None
    )

    result = run_daily(settings, trigger="test")

    assert result.status == "OK", result.errors
    assert result.events_ingested == 1
    assert result.signals_opened == 1      # SUE über der Schwelle

    with session_scope() as session:
        event = session.scalar(select(EarningsEvent))
        assert event.source == "yahoo"
        assert event.report_hour is None   # unbekannt -> Einstieg am Folgetag
        assert event.sue is not None and event.sue > 1.0

        signal = session.scalar(select(Signal))
        # Unbekannter Meldezeitpunkt: nie am Meldetag selbst einsteigen.
        assert signal.entry_date > report_date


def test_ausweichquelle_greift_auch_ohne_erneuten_finnhub_versuch(
    settings, database, monkeypatch, trading_days, report_date
):
    """Beim zweiten Lauf ist Finnhub gesperrt - Yahoo muss trotzdem liefern."""
    from app.pipeline import run_daily
    from app.sources.earnings import YAHOO, RawEarnings as SharedRaw

    finnhub_calls: list[str] = []
    yahoo_calls: list[str] = []

    class ForbiddenFinnhub(FakeFinnhubBase):
        def company_profile(self, symbol):
            finnhub_calls.append("profile")
            raise _forbidden("/stock/profile2")

        def earnings_calendar(self, *_a, **_k):
            finnhub_calls.append("calendar")
            raise _forbidden("/calendar/earnings")

        def earnings_surprises(self, *_a, **_k):
            finnhub_calls.append("earnings")
            raise _forbidden("/stock/earnings")

        def recommendations(self, *_a, **_k):
            finnhub_calls.append("reco")
            raise _forbidden("/stock/recommendation")

    def fake_yahoo(symbol, limit=24):
        yahoo_calls.append(symbol)
        return [SharedRaw(symbol, report_date, 1.00, 1.20, 20.0, "q", None, YAHOO)]

    monkeypatch.setattr("app.pipeline.FinnhubClient", ForbiddenFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", lambda s, a, b: ([], []))
    monkeypatch.setattr("app.pipeline.yahoo_earnings.fetch_earnings", fake_yahoo)
    monkeypatch.setattr(
        "app.pipeline.yahoo_earnings.fetch_next_earnings_date", lambda s: None
    )

    run_daily(settings, trigger="test")
    finnhub_nach_erstem = len(finnhub_calls)

    run_daily(settings, trigger="test")

    assert len(finnhub_calls) == finnhub_nach_erstem   # keine neuen Fehlversuche
    assert len(yahoo_calls) == 2                       # Ausweichquelle läuft weiter


def test_abgeschaltete_ausweichquelle_laesst_den_titel_leer(
    settings, database, monkeypatch, report_date
):
    import dataclasses

    from app.models import EarningsEvent
    from app.pipeline import run_daily

    class ForbiddenFinnhub(FakeFinnhubBase):
        def company_profile(self, symbol):
            raise _forbidden("/stock/profile2")

        def earnings_calendar(self, *_a, **_k):
            raise _forbidden("/calendar/earnings")

        def earnings_surprises(self, *_a, **_k):
            raise _forbidden("/stock/earnings")

        def recommendations(self, *_a, **_k):
            raise _forbidden("/stock/recommendation")

    def darf_nicht_aufgerufen_werden(*_a, **_k):
        raise AssertionError("Ausweichquelle trotz earnings_fallback_enabled=False")

    monkeypatch.setattr("app.pipeline.FinnhubClient", ForbiddenFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", lambda s, a, b: ([], []))
    monkeypatch.setattr(
        "app.pipeline.yahoo_earnings.fetch_earnings", darf_nicht_aufgerufen_werden
    )

    aus = dataclasses.replace(settings, earnings_fallback_enabled=False)
    result = run_daily(aus, trigger="test")

    assert result.status == "OK"
    with session_scope() as session:
        assert session.scalar(select(EarningsEvent)) is None
