"""Rueckwirkende Momentum-Rangfolgen aus gespeicherten Kursen."""
import datetime as dt

import pytest

from app.retro import momentum_history, rebalance_dates, summarize

START = dt.date(2024, 1, 1)


def werktage(anzahl: int, ab: dt.date = START) -> list[dt.date]:
    tage, tag = [], ab
    while len(tage) < anzahl:
        if tag.weekday() < 5:
            tage.append(tag)
        tag += dt.timedelta(days=1)
    return tage


def universum(anzahl_titel: int = 10, tage: int = 600) -> dict:
    """Titel T0..T9 mit konstantem Tageszuwachs; T9 steigt am staerksten."""
    kalender = werktage(tage)
    out = {}
    for i in range(anzahl_titel):
        wachstum = 1 + (i - 4) * 0.0005        # T0..T3 fallen, T5.. steigen
        kurse = [100.0 * wachstum ** n for n in range(tage)]
        out[f"T{i}"] = (kalender, kurse)
    return out


def rechne(series, start=None, end=None, **over):
    kalender = next(iter(series.values()))[0]
    args = dict(
        lookback_days=252, skip_days=21, holding_period_days=21, group_fraction=0.3,
        min_universe=8, start=start or kalender[300], end=end or kalender[-1] + dt.timedelta(days=1),
    )
    args.update(over)
    return momentum_history(series, **args)


def test_umschichtung_am_ersten_handelstag_jedes_monats():
    tage = werktage(70, dt.date(2026, 1, 1))
    daten = rebalance_dates(tage, dt.date(2026, 1, 1), dt.date(2026, 4, 1))
    assert daten == [dt.date(2026, 1, 1), dt.date(2026, 2, 2), dt.date(2026, 3, 2)]


def test_staerkster_titel_ist_long_schwaechster_short():
    hist = rechne(universum())
    monat = hist.months[0]
    longs = {p.symbol for p in monat.positions if p.direction == "LONG"}
    shorts = {p.symbol for p in monat.positions if p.direction == "SHORT"}
    assert longs == {"T9", "T8", "T7"}
    assert shorts == {"T0", "T1", "T2"}


def test_rangfolge_kennt_die_zukunft_nicht():
    """Der Kern: was nach dem Umschichtungstag passiert, darf die Rangfolge
    dieses Tages nicht aendern."""
    series = universum()
    vorher = rechne(series)
    tag = vorher.months[0].rebalance_date

    kalender, kurse = series["T0"]
    # T0 explodiert NACH dem Umschichtungstag.
    series["T0"] = (kalender, [k * (50 if d > tag else 1) for d, k in zip(kalender, kurse)])
    nachher = rechne(series)

    assert [(p.symbol, p.direction) for p in nachher.months[0].positions] == \
           [(p.symbol, p.direction) for p in vorher.months[0].positions]


def test_ein_und_ausstieg_wie_live():
    series = universum()
    hist = rechne(series)
    p = hist.months[0].positions[0]
    kalender, kurse = series[p.symbol]
    i = kalender.index(p.entry_date)
    assert p.entry_date == hist.months[0].rebalance_date
    assert p.exit_date == kalender[i + 21]
    assert p.return_pct == pytest.approx((kurse[i + 21] - kurse[i]) / kurse[i])


def test_short_gewinnt_wenn_der_kurs_faellt():
    hist = rechne(universum())
    short = [p for p in hist.months[0].positions if p.symbol == "T0"][0]
    assert short.direction == "SHORT" and short.return_pct > 0


def test_letzter_monat_laeuft_noch():
    hist = rechne(universum())
    letzte = hist.months[-1].positions
    assert letzte and all(p.exit_date is None and p.return_pct is None for p in letzte)
    assert all(p.unrealized_pct is not None for p in letzte)


def test_zu_kleines_universum_wird_nicht_rangiert():
    series = {k: v for k, v in universum().items() if k in {"T0", "T1", "T2", "T3", "T4"}}
    hist = rechne(series)
    assert hist.months and all(not m.positions for m in hist.months)
    assert "mindestens 8" in hist.months[0].reason
    assert hist.covered_from is None


def test_zu_kurze_historie_am_anfang():
    series = universum(tage=600)
    kalender = series["T0"][0]
    hist = rechne(series, start=kalender[100])
    assert not hist.months[0].positions          # erst ab 253 Kurstagen
    assert hist.covered_from is not None and hist.covered_from >= kalender[252]


def test_vergleich_ohne_den_titel_selbst():
    hist = rechne(universum())
    p = [p for p in hist.months[0].positions if p.symbol == "T9"][0]
    series = universum()
    kalender = series["T9"][0]
    i, j = kalender.index(p.entry_date), kalender.index(p.exit_date)
    andere = [(k[j] - k[i]) / k[i] for s, (_, k) in series.items() if s != "T9"]
    assert p.benchmark_return_pct == pytest.approx(sum(andere) / len(andere))


def test_titel_je_symbol():
    hist = rechne(universum())
    assert hist.for_symbol("T4") == []          # Mittelfeld, nie in einer Gruppe
    assert len(hist.for_symbol("T9")) == len(hist.months)


def test_kennzahlen_im_format_der_live_statistik():
    hist = rechne(universum())
    k = summarize(hist.positions)
    assert k["origin"] == "retro"
    assert k["closed_count"] == sum(1 for p in hist.positions if p.return_pct is not None)
    assert k["open_count"] == len(hist.months[-1].positions)
    assert k["win_count"] == k["closed_count"]     # konstante Trends: alles trifft
    assert 0 <= k["beat_count"] <= k["beat_base"]


@pytest.mark.parametrize("heute,erwartet", [
    (dt.date(2026, 9, 23), dt.date(2025, 10, 1)),
    (dt.date(2026, 1, 5), dt.date(2025, 2, 1)),
    (dt.date(2026, 12, 31), dt.date(2026, 1, 1)),
])
def test_zwoelf_monate_einschliesslich_des_laufenden(heute, erwartet):
    from app.momentum_view import _months_back

    assert _months_back(heute, 12) == erwartet


def test_rueckrechnung_endet_vor_dem_monat_der_ersten_live_umschichtung(tmp_path):
    """Sonst stuenden im Startmonat eine rueckwirkende und eine echte Position nebeneinander."""
    import app.db as db_module
    from app.config import Momentum, Settings
    from app.db import init_engine, session_scope
    from app.models import MomentumRebalance, Price, Ticker
    from app.momentum_view import retro_history

    init_engine(tmp_path / "r.db")
    try:
        series = universum(tage=600)
        with session_scope() as session:
            for sym, (tage, kurse) in series.items():
                session.add(Ticker(symbol=sym, active=True))
                session.flush()
                for d, k in zip(tage, kurse):
                    session.add(Price(symbol=sym, date=d, close=k))
            letzter = series["T0"][0][-1]
            live = letzter.replace(day=15) if letzter.day > 15 else letzter
            session.add(MomentumRebalance(
                period_key=f"{live:%Y-%m}", rebalance_date=live, universe_size=10,
                group_size=3, lookback_days=252, skip_days=21))
        settings = Settings(tickers=(), finnhub_api_key="x", db_path=tmp_path / "r.db",
                            momentum=Momentum(enabled=True))
        with session_scope() as session:
            hist = retro_history(session, settings, today=letzter)
        assert hist.months
        assert all(m.rebalance_date < live.replace(day=1) for m in hist.months)
    finally:
        db_module._engine = None
        db_module._SessionLocal = None


def test_monatliche_rangfolge_umfasst_auch_das_mittelfeld():
    from app.retro import monthly_ranks

    series = universum()
    kalender = series["T0"][0]
    monate = monthly_ranks(series, lookback_days=252, skip_days=21, group_fraction=0.3,
                           min_universe=8, start=kalender[300], end=kalender[-1])
    assert monate
    assert monate[0].ranks["T9"] == 1 and monate[0].ranks["T4"] == 6
    assert len(monate[0].ranks) == 10 and monate[0].group_size == 3


def test_momentum_karte_mit_rangleiter_und_verlauf(tmp_path):
    import app.db as db_module
    from app.config import Momentum, Settings
    from app.db import init_engine, session_scope
    from app.models import Price, Ticker
    from app.momentum_view import symbol_card

    init_engine(tmp_path / "k.db")
    try:
        # Kalender endet heute, damit "heute" als letzter Punkt passt.
        heute = dt.date.today()
        tage = [d for d in (heute - dt.timedelta(days=i) for i in range(900)) if d.weekday() < 5][::-1]
        with session_scope() as session:
            for i in range(10):
                wachstum = 1 + (i - 4) * 0.0005
                session.add(Ticker(symbol=f"T{i}", active=True))
                session.flush()
                for n, d in enumerate(tage):
                    session.add(Price(symbol=f"T{i}", date=d, close=100.0 * wachstum ** n))
        settings = Settings(tickers=(), finnhub_api_key="x", db_path=tmp_path / "k.db",
                            momentum=Momentum(enabled=True))
        with session_scope() as session:
            card = symbol_card(session, settings, "T9")
        assert (card.rank, card.universe, card.group, card.zone) == (1, 10, 3, "top")
        assert [c.zone for c in card.ladder] == ["top"] * 3 + ["middle"] * 4 + ["bottom"] * 3
        assert sum(c.own for c in card.ladder) == 1
        assert len(card.history) >= 12
        assert all(p.y == card.history[0].y for p in card.history)    # immer Rang 1 = oben
        assert card.band_top < card.band_bottom
    finally:
        db_module._engine = None
        db_module._SessionLocal = None
