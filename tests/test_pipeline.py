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
    """Leere DB mit dem Testtitel, dessen einmaliger Earnings-Rueckblick schon lief.

    Die Tests hier pruefen den Tagesbetrieb. Der Rueckblick ueber ein Jahr
    haette sonst beim ersten Lauf jedes Tests zusaetzliche Quartale geholt;
    er hat eigene Tests weiter unten.
    """
    from app.models import Ticker
    from app.pipeline import RETRO_EARNINGS_DAYS

    init_engine(settings.db_path)
    with session_scope() as session:
        session.add(Ticker(symbol=SYMBOL, active=True,
                           earnings_history_days=RETRO_EARNINGS_DAYS))
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


# -- Kursabruf je Titel -------------------------------------------------------


def _recording_prices(trading_days, calls):
    def fetch(symbols, start, end):
        calls.append((tuple(symbols), start))
        return [
            PriceRow(sym, d, 100.0, 101.0, 99.0, 100.0 + i, 1000)
            for sym in symbols
            for i, d in enumerate(trading_days)
            if start <= d <= end
        ], []
    return fetch


def test_neuer_titel_bekommt_volle_historie_trotz_bestand(settings, database, monkeypatch,
                                                          trading_days):
    """Frueher entschied ein einziger vorhandener Kurs in der DB ueber das Fenster -
    ein spaeter aufgenommener Titel bekam dann nur eine Woche Historie."""
    from app import watchlist
    from app.pipeline import run_daily

    calls: list = []
    monkeypatch.setattr("app.pipeline.FinnhubClient", FakeFinnhubBase)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, calls))

    run_daily(settings, trigger="test")
    with session_scope() as session:
        watchlist.add(session, ["NEU"])
    calls.clear()
    run_daily(settings, trigger="test")

    today = dt.date.today()
    fenster = dict(calls)
    assert fenster[("NEU",)] == today - dt.timedelta(days=settings.price_backfill_days)
    assert fenster[(SYMBOL,)] == today - dt.timedelta(days=settings.price_refresh_days)


def test_entfernter_titel_mit_offener_position_bekommt_weiter_kurse(
    settings, database, monkeypatch, trading_days
):
    """Sonst schloesse die Position nie - sie bliebe fuer immer OPEN."""
    from app import watchlist
    from app.pipeline import _price_symbols, run_daily

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

    calls: list = []
    monkeypatch.setattr("app.pipeline.FinnhubClient", RecentFinnhub)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, calls))
    assert run_daily(settings, trigger="test").signals_opened == 1

    with session_scope() as session:
        removal = watchlist.remove(session, SYMBOL)
    assert removal.open_pead == 1

    with session_scope() as session:
        assert watchlist.active_symbols(session) == []
        assert _price_symbols(session) == [SYMBOL]

    calls.clear()
    run_daily(settings, trigger="test")
    assert any(SYMBOL in symbols for symbols, _ in calls)


def test_entfernter_titel_ohne_position_bekommt_keine_kurse_mehr(
    settings, database, monkeypatch, trading_days
):
    from app import watchlist
    from app.pipeline import _price_symbols, run_daily

    monkeypatch.setattr("app.pipeline.FinnhubClient", FakeFinnhubBase)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, []))
    run_daily(settings, trigger="test")

    with session_scope() as session:
        watchlist.remove(session, SYMBOL)
    with session_scope() as session:
        assert _price_symbols(session) == []


# -- Lauf nach Aenderung der Titelliste -------------------------------------


def _warte_auf_hintergrund(timeout=10.0):
    import time

    from app.pipeline import background_state

    ende = time.time() + timeout
    while background_state() is not None and time.time() < ende:
        time.sleep(0.02)
    assert background_state() is None, "Hintergrundlauf haengt"


def test_hintergrundlauf_holt_daten_fuer_neuen_titel(settings, database, monkeypatch,
                                                     trading_days):
    from app import watchlist
    from app.models import PipelineRun
    from app.pipeline import request_background_run, run_daily

    monkeypatch.setattr("app.pipeline.FinnhubClient", FakeFinnhubBase)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, []))
    run_daily(settings, trigger="test")
    with session_scope() as session:
        watchlist.add(session, ["NEU"])

    assert request_background_run(settings) is True
    _warte_auf_hintergrund()

    with session_scope() as session:
        assert session.scalar(select(PipelineRun.trigger).order_by(PipelineRun.id.desc())) == "titel"
        assert session.scalar(select(Price).where(Price.symbol == "NEU")) is not None


def test_hintergrundlauf_wartet_auf_laufenden_und_buendelt(settings, database, monkeypatch,
                                                          trading_days):
    """Waehrend ein Lauf laeuft, reihen zwei Aenderungen genau einen Folgelauf ein."""
    import app.pipeline as pipeline
    from app.models import PipelineRun

    monkeypatch.setattr("app.pipeline.FinnhubClient", FakeFinnhubBase)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, []))

    assert pipeline._run_lock.acquire(blocking=False)   # "laufender" Lauf
    try:
        assert pipeline.request_background_run(settings) is True
        assert pipeline.request_background_run(settings) is False
        assert pipeline.background_state() == "queued"
    finally:
        pipeline._run_lock.release()
    _warte_auf_hintergrund()

    with session_scope() as session:
        triggers = session.scalars(select(PipelineRun.trigger)).all()
    assert triggers == ["titel"]


def test_manueller_lauf_meldet_besetzt_statt_zu_warten(settings, database):
    import app.pipeline as pipeline

    assert pipeline._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(pipeline.PipelineBusy):
            pipeline.run_daily(settings, trigger="manual")
    finally:
        pipeline._run_lock.release()


# -- Kurse des laufenden Handelstags ------------------------------------------


def test_zwischenstand_von_heute_wird_nicht_gespeichert(settings, database, monkeypatch,
                                                        trading_days):
    """Sonst schloesse ein Lauf um 11 Uhr eine Position zum Vormittagskurs."""
    from app.pipeline import run_daily

    monkeypatch.setattr("app.pipeline.trading_day_complete", lambda now=None: False)
    monkeypatch.setattr("app.pipeline.FinnhubClient", FakeFinnhubBase)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, []))
    run_daily(settings, trigger="test")

    with session_scope() as session:
        letzter = session.scalar(select(Price.date).order_by(Price.date.desc()))
    assert letzter == trading_days[-2]


def test_nach_handelsschluss_zaehlt_der_heutige_kurs(settings, database, monkeypatch,
                                                     trading_days):
    from app.pipeline import run_daily

    monkeypatch.setattr("app.pipeline.FinnhubClient", FakeFinnhubBase)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, []))
    run_daily(settings, trigger="test")

    with session_scope() as session:
        letzter = session.scalar(select(Price.date).order_by(Price.date.desc()))
    assert letzter == trading_days[-1]


@pytest.mark.parametrize("uhrzeit,erwartet", [
    ("09:45", False),
    ("21:30", False),     # Xetra zu, New York noch offen
    ("22:14", False),
    ("22:15", True),
    ("23:59", True),
])
def test_handelsschluss_nach_deutscher_zeit(monkeypatch, uhrzeit, erwartet):
    monkeypatch.undo()    # die Vorgabe aus conftest.py aufheben
    from app.pipeline import BERLIN, trading_day_complete

    stunde, minute = map(int, uhrzeit.split(":"))
    now = BERLIN.localize(dt.datetime(2026, 9, 23, stunde, minute))
    assert trading_day_complete(now) is erwartet


def test_handelsschluss_rechnet_utc_um(monkeypatch):
    monkeypatch.undo()
    from app.pipeline import trading_day_complete

    # 20:30 UTC = 22:30 Sommerzeit in Berlin
    assert trading_day_complete(dt.datetime(2026, 9, 23, 20, 30, tzinfo=dt.timezone.utc))
    assert not trading_day_complete(dt.datetime(2026, 9, 23, 19, 30, tzinfo=dt.timezone.utc))


# -- Einmaliger Rueckblick je Titel ------------------------------------------


def _kalender_rekorder(fenster):
    class Rekorder(FakeFinnhubBase):
        def earnings_calendar(self, symbol, date_from, date_to):
            fenster.append((symbol, date_from))
            return []
    return Rekorder


def test_neuer_titel_bekommt_einmal_ein_jahr_earnings(settings, database, monkeypatch,
                                                     trading_days):
    from app import watchlist
    from app.models import Ticker
    from app.pipeline import RETRO_EARNINGS_DAYS, run_daily

    fenster: list = []
    monkeypatch.setattr("app.pipeline.FinnhubClient", _kalender_rekorder(fenster))
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, []))
    with session_scope() as session:
        watchlist.add(session, ["NEU"])

    run_daily(settings, trigger="test")
    heute = dt.date.today()
    erster = dict(fenster)
    assert erster["NEU"] == heute - dt.timedelta(days=RETRO_EARNINGS_DAYS)
    assert erster[SYMBOL] == heute - dt.timedelta(days=settings.lookback_days_earnings)
    with session_scope() as session:
        assert session.get(Ticker, "NEU").earnings_history_days == RETRO_EARNINGS_DAYS

    fenster.clear()
    run_daily(settings, trigger="test")
    assert dict(fenster)["NEU"] == heute - dt.timedelta(days=settings.lookback_days_earnings)


def test_gescheiterter_rueckblick_wird_wiederholt(settings, database, monkeypatch,
                                                 trading_days):
    from app import watchlist
    from app.models import Ticker
    from app.pipeline import run_daily
    from app.sources.finnhub_client import FinnhubTransientError

    class Wackelig(FakeFinnhubBase):
        def earnings_calendar(self, symbol, *_a):
            if symbol == "NEU":
                raise FinnhubTransientError("Timeout")
            return []

    monkeypatch.setattr("app.pipeline.FinnhubClient", Wackelig)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, []))
    with session_scope() as session:
        watchlist.add(session, ["NEU"])
    run_daily(settings, trigger="test")
    with session_scope() as session:
        assert session.get(Ticker, "NEU").earnings_history_days is None


def test_rueckwirkende_signale_sind_als_solche_erkennbar(settings, database, monkeypatch,
                                                        trading_days):
    """Ein Jahr zurueckgeholte Meldungen erzeugen Signale - aber nie Live-Signale."""
    from app import watchlist
    from app.pipeline import run_daily
    from app.views import summary

    alt = trading_days[5]

    class Historisch(FakeFinnhubBase):
        def earnings_calendar(self, symbol, date_from, date_to):
            if symbol != "NEU" or not (date_from <= alt <= date_to):
                return []
            return [RawEarnings(symbol, alt, 1.00, 1.30, 30.0, "q", "amc")]

    monkeypatch.setattr("app.pipeline.FinnhubClient", Historisch)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, []))
    with session_scope() as session:
        watchlist.add(session, ["NEU"])
    result = run_daily(settings, trigger="test")
    assert result.signals_opened == 1

    with session_scope() as session:
        signal = session.scalar(select(Signal))
        assert signal.retro is True
        assert signal.status == "CLOSED"          # Haltedauer laengst vorbei
        assert summary(session)["closed_count"] == 0
        assert summary(session, retro=True)["closed_count"] == 1


def test_erhoehte_kurstiefe_wird_einmal_nachgeholt(settings, database, monkeypatch,
                                                  trading_days):
    """Wer price_backfill_days erhoeht, braucht kein 'make backfill' mehr."""
    import dataclasses

    from app.pipeline import run_daily

    calls: list = []
    monkeypatch.setattr("app.pipeline.FinnhubClient", FakeFinnhubBase)
    monkeypatch.setattr("app.pipeline.fetch_prices", _recording_prices(trading_days, calls))
    run_daily(settings, trigger="test")
    tiefer = dataclasses.replace(settings, price_backfill_days=settings.price_backfill_days + 300)

    calls.clear()
    run_daily(tiefer, trigger="test")
    heute = dt.date.today()
    assert dict(calls)[(SYMBOL,)] == heute - dt.timedelta(days=tiefer.price_backfill_days)

    calls.clear()
    run_daily(tiefer, trigger="test")
    assert dict(calls)[(SYMBOL,)] == heute - dt.timedelta(days=tiefer.price_refresh_days)
