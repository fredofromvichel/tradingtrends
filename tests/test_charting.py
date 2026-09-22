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
    assert set(data[0]) == {"x", "y", "date", "close"}
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
