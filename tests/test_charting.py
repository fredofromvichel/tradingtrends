"""Tests der Diagrammgeometrie - ohne Rendering."""

from __future__ import annotations

import datetime as dt

import pytest

from app.charting import HEIGHT, PAD_BOTTOM, PAD_LEFT, PAD_TOP, build_price_chart
from app.indicators import PricePoint


def series(values: list[float], start: dt.date = dt.date(2026, 1, 5)) -> list[PricePoint]:
    points: list[PricePoint] = []
    day = start
    for value in values:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        points.append(PricePoint(day, value, 1000))
        day += dt.timedelta(days=1)
    return points


def test_leere_reihe_ergibt_leeres_diagramm():
    assert build_price_chart([]).empty is True
    assert build_price_chart(series([100.0])).empty is True


def test_punkte_liegen_innerhalb_der_zeichenflaeche():
    chart = build_price_chart(series([100 + i for i in range(40)]))
    assert chart.empty is False
    for point in chart.hover:
        assert PAD_LEFT - 0.01 <= point.x <= chart.plot_right + 0.01
        assert PAD_TOP - 0.01 <= point.y <= HEIGHT - PAD_BOTTOM + 0.01


def test_hoher_kurs_liegt_weiter_oben():
    """Y waechst nach unten - der teuerste Tag braucht das kleinste Y."""
    chart = build_price_chart(series([100, 130, 110, 90]))
    y_by_close = {p.close: p.y for p in chart.hover}
    assert y_by_close[130] < y_by_close[110] < y_by_close[90]


def test_konstanter_kurs_bricht_nicht():
    chart = build_price_chart(series([100.0] * 20))
    assert chart.empty is False
    assert len(chart.hover) == 20


def test_achsenbeschriftung_nutzt_runde_werte():
    chart = build_price_chart(series([100 + i for i in range(60)]))
    labels = [t.label for t in chart.y_ticks]
    assert labels
    for label in labels:
        assert "," not in label            # Tausender mit schmalem Leerzeichen
    # Runde Schritte, keine krummen Zwischenwerte wie 103.47
    numbers = [float(t.label.replace(" ", "")) for t in chart.y_ticks]
    steps = {round(b - a, 6) for a, b in zip(numbers, numbers[1:])}
    assert len(steps) == 1


def test_marker_landet_auf_dem_richtigen_tag():
    points = series([100 + i for i in range(30)])
    target = points[10].date
    chart = build_price_chart(points, [(target, "BUY", "Kaufsignal")])
    assert len(chart.markers) == 1
    marker = chart.markers[0]
    assert marker.date == target.isoformat()
    assert marker.kind == "BUY"
    assert marker.x == pytest.approx(chart.hover[10].x)
    assert marker.y == pytest.approx(chart.hover[10].y)


def test_marker_am_wochenende_rueckt_auf_den_naechsten_handelstag():
    points = series([100 + i for i in range(30)], start=dt.date(2026, 1, 5))
    samstag = dt.date(2026, 1, 10)
    chart = build_price_chart(points, [(samstag, "SELL", "Verkaufssignal")])
    assert len(chart.markers) == 1
    assert dt.date.fromisoformat(chart.markers[0].date) > samstag


def test_marker_nach_dem_letzten_kurstag_wird_verworfen():
    """Sonst zeichnete er ausserhalb der Flaeche."""
    points = series([100 + i for i in range(10)])
    chart = build_price_chart(points, [(dt.date(2030, 1, 1), "BUY", "später")])
    assert chart.markers == ()


def test_alle_markerarten_werden_uebernommen():
    points = series([100 + i for i in range(30)])
    events = [
        (points[3].date, "BUY", "Kauf"),
        (points[12].date, "SELL", "Verkauf"),
        (points[20].date, "NEUTRAL", "Meldung ohne Signal"),
    ]
    chart = build_price_chart(points, events)
    assert [m.kind for m in chart.markers] == ["BUY", "SELL", "NEUTRAL"]


def test_endpunkt_entspricht_dem_letzten_kurs():
    points = series([100 + i for i in range(25)])
    chart = build_price_chart(points)
    assert chart.last_x == pytest.approx(chart.hover[-1].x)
    assert chart.last_y == pytest.approx(chart.hover[-1].y)
    assert "124" in chart.last_label


def test_hover_daten_sind_json_tauglich():
    chart = build_price_chart(series([100 + i for i in range(10)]))
    data = chart.hover_data
    assert len(data) == 10
    assert set(data[0]) == {"x", "y", "date", "close", "sma50", "sma200"}
    assert isinstance(data[0]["date"], str)


def test_randticks_ragen_nicht_aus_der_flaeche():
    """Mittig zentrierter Text am Rand wuerde links/rechts abgeschnitten."""
    chart = build_price_chart(series([100 + (i % 20) for i in range(300)]))
    assert chart.x_ticks[0].anchor == "start"
    assert chart.x_ticks[-1].anchor == "end"
    for tick in chart.x_ticks[1:-1]:
        assert tick.anchor == "middle"


def test_x_achse_bleibt_lesbar_bei_langer_historie():
    chart = build_price_chart(series([100 + (i % 20) for i in range(400)]))
    assert 0 < len(chart.x_ticks) <= 7


def test_achsenmarke_weicht_dem_endwert_aus():
    """Sonst überlagern sich Achsenbeschriftung und direkt beschrifteter Endwert."""
    from app.charting import LABEL_MIN_GAP

    chart = build_price_chart(series([100 + i * 0.5 for i in range(80)]))
    assert chart.last_y is not None
    for tick in chart.y_ticks:
        assert abs(tick.position - chart.last_y) >= LABEL_MIN_GAP


# -- Spuren fuer Signalphasen -------------------------------------------------

from app.charting import LANES, MIN_BAR_WIDTH, PhaseInput, rolling_sma  # noqa: E402


def phase(entry, exit_, lane="pead", side="long", retro=False, result=0.05):
    return PhaseInput(lane=lane, side=side, label="BUY" if side == "long" else "SELL",
                      retro=retro, entry=entry, exit=exit_, result=result,
                      benchmark=0.01, note="SUE +1,50")


def test_phase_liegt_in_ihrer_spur_unter_der_kursflaeche():
    points = series([100 + i for i in range(60)])
    chart = build_price_chart(points, phases=[phase(points[10].date, points[30].date)])
    p = chart.phases[0]
    assert p.y == LANES["pead"] and p.y > HEIGHT - PAD_BOTTOM
    assert p.x0 < p.x1


def test_phasen_beider_strategien_ueberlappen_nicht():
    points = series([100 + i for i in range(60)])
    chart = build_price_chart(points, phases=[
        phase(points[10].date, points[30].date),
        phase(points[10].date, points[30].date, lane="momentum", side="short"),
    ])
    pead, mom = chart.phases
    assert pead.y + pead.height <= mom.y


def test_offene_phase_reicht_bis_zum_rechten_rand():
    points = series([100 + i for i in range(60)])
    chart = build_price_chart(points, phases=[phase(points[40].date, None)])
    assert chart.phases[0].open
    assert chart.phases[0].x1 == chart.hover[-1].x


def test_phase_vor_dem_diagramm_wird_angeschnitten():
    points = series([100 + i for i in range(60)])
    frueher = points[0].date - dt.timedelta(days=20)
    chart = build_price_chart(points, phases=[phase(frueher, points[5].date)])
    p = chart.phases[0]
    assert p.clipped and p.x0 == PAD_LEFT


def test_phase_ganz_ausserhalb_entfaellt():
    points = series([100 + i for i in range(60)])
    alt = points[0].date - dt.timedelta(days=100)
    chart = build_price_chart(points, phases=[phase(alt, alt + dt.timedelta(days=28))])
    assert chart.phases == ()


def test_kurze_phase_bleibt_sichtbar():
    points = series([100 + i for i in range(250)])
    chart = build_price_chart(points, phases=[phase(points[10].date, points[10].date)])
    assert chart.phases[0].x1 - chart.phases[0].x0 >= MIN_BAR_WIDTH


def test_rueckwirkend_bleibt_erkennbar():
    points = series([100 + i for i in range(60)])
    chart = build_price_chart(
        points,
        events=[(points[10].date, "BUY", "rückwirkend", True)],
        phases=[phase(points[10].date, points[30].date, retro=True)],
    )
    assert chart.phases[0].retro and chart.markers[0].retro
    assert chart.phase_data[0]["retro"] is True


def test_unbekannte_spur_wird_ignoriert():
    points = series([100 + i for i in range(60)])
    chart = build_price_chart(points, phases=[phase(points[1].date, points[5].date, lane="x")])
    assert chart.phases == ()


# -- Gleitende Durchschnitte ----------------------------------------------------


def test_gleitender_durchschnitt():
    assert rolling_sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_durchschnitt_nutzt_die_laengere_historie():
    """Der 200-Tage-Schnitt am ersten Diagrammtag braucht die 199 Kurse davor."""
    lang = series([100 + i for i in range(460)])
    chart = build_price_chart(lang[-252:], history=lang)
    erster = 460 - 252
    erwartet = sum(p.close for p in lang[erster - 199: erster + 1]) / 200
    assert chart.hover[0].sma200 == pytest.approx(erwartet)
    assert {r.key for r in chart.ref_lines} == {"sma50", "sma200"}


def test_ticker_laedt_genug_kurse_fuer_die_200er_linie():
    from app.ticker_view import CHART_BARS, CONTEXT_BARS

    assert CONTEXT_BARS >= CHART_BARS + 199


def test_ohne_ausreichende_historie_keine_200er_linie():
    chart = build_price_chart(series([100 + i for i in range(120)]))
    assert {r.key for r in chart.ref_lines} == {"sma50"}


def test_achse_fasst_auch_die_durchschnitte():
    """Nach einem Absturz liegt der 200er weit ueber dem Kurs - er darf nicht
    aus dem Bild laufen."""
    werte = [200.0] * 300 + [100.0] * 100
    lang = series(werte)
    chart = build_price_chart(lang[-60:], history=lang)
    sma = next(r for r in chart.ref_lines if r.key == "sma200")
    ys = [float(pt.split(",")[1]) for pt in sma.points.split()]
    assert min(ys) >= PAD_TOP - 0.01


def test_spanne_markiert_hoch_und_tief():
    chart = build_price_chart(series([100, 130, 110, 90]))
    assert chart.high_y < chart.low_y
    assert "130" in chart.high_label and "90" in chart.low_label
