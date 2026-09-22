"""Geometrie des Kursverlaufs - reine Rechnung, kein Rendering.

Erzeugt aus einer Kursreihe die Koordinaten fuer ein einzelnes Liniendiagramm
mit Ereignismarkern. Eine Serie, eine Achse; die Marker tragen ihre Identitaet
ueber die Form (Dreieck auf / Dreieck ab / offener Kreis), nicht allein ueber
die Farbe.

Frei von Datenbank- und Netzzugriffen (siehe tests/test_charting.py).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from app.indicators import PricePoint

# Zeichenflaeche in Nutzer-Einheiten; das SVG skaliert per viewBox.
WIDTH = 720
HEIGHT = 260
PAD_LEFT = 8
PAD_RIGHT = 62      # Platz fuer das Endwert-Label
PAD_TOP = 14
PAD_BOTTOM = 26     # Platz fuer die Datumsachse
# Mindestabstand zweier Beschriftungen auf der Y-Achse, in Nutzer-Einheiten.
LABEL_MIN_GAP = 13

MONTHS_DE = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
             "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")


@dataclass(frozen=True)
class Marker:
    x: float
    y: float
    kind: str           # "BUY" | "SELL" | "NEUTRAL"
    label: str
    date: str
    close: float


@dataclass(frozen=True)
class Tick:
    position: float
    label: str
    # "start" | "middle" | "end" - haelt den ersten und letzten Datumstick
    # innerhalb der Zeichenflaeche.
    anchor: str = "middle"


@dataclass(frozen=True)
class HoverPoint:
    x: float
    y: float
    date: str
    close: float


@dataclass(frozen=True)
class PriceChart:
    width: int = WIDTH
    height: int = HEIGHT
    plot_left: float = PAD_LEFT
    plot_right: float = WIDTH - PAD_RIGHT
    plot_top: float = PAD_TOP
    plot_bottom: float = HEIGHT - PAD_BOTTOM
    line: str = ""                       # polyline-Punkte
    area: str = ""                       # geschlossener Pfad fuer die Lasur
    markers: tuple[Marker, ...] = ()
    y_ticks: tuple[Tick, ...] = ()
    x_ticks: tuple[Tick, ...] = ()
    hover: tuple[HoverPoint, ...] = ()
    last_x: float | None = None
    last_y: float | None = None
    last_label: str = ""
    empty: bool = True

    @property
    def hover_data(self) -> list[dict]:
        """Punkte fuer das Fadenkreuz, JSON-tauglich."""
        return [
            {"x": h.x, "y": h.y, "date": h.date, "close": h.close} for h in self.hover
        ]


def _nice_step(span: float, target_ticks: int = 4) -> float:
    """Achsenschritt auf eine runde Zahl bringen (1 / 2 / 2.5 / 5 x 10^k)."""
    if span <= 0:
        return 1.0
    raw = span / max(1, target_ticks)
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _format_price(value: float) -> str:
    digits = 2 if abs(value) < 100 else (1 if abs(value) < 1000 else 0)
    return f"{value:,.{digits}f}".replace(",", " ")


def _format_date(day: dt.date) -> str:
    return f"{MONTHS_DE[day.month - 1]} {day.year % 100:02d}"


def build_price_chart(
    points: list[PricePoint],
    events: list[tuple[dt.date, str, str]] | None = None,
) -> PriceChart:
    """Baut die Zeichengeometrie.

    ``events`` sind Tupel aus (Datum, Art, Beschriftung); die Art ist "BUY",
    "SELL" oder "NEUTRAL". Ereignisse ausserhalb der Kursreihe werden auf den
    naechstgelegenen vorhandenen Handelstag gelegt, damit ein Marker nie
    ausserhalb der Flaeche landet.
    """
    if len(points) < 2:
        return PriceChart()

    closes = [p.close for p in points]
    low, high = min(closes), max(closes)
    if high == low:
        high = low + max(1.0, abs(low) * 0.01)

    step = _nice_step(high - low)
    axis_low = math.floor(low / step) * step
    axis_high = math.ceil(high / step) * step
    if axis_high <= axis_low:
        axis_high = axis_low + step

    left, right = PAD_LEFT, WIDTH - PAD_RIGHT
    top, bottom = PAD_TOP, HEIGHT - PAD_BOTTOM
    span_x = right - left
    span_y = bottom - top

    def to_x(index: int) -> float:
        return left + span_x * index / (len(points) - 1)

    def to_y(value: float) -> float:
        return bottom - span_y * (value - axis_low) / (axis_high - axis_low)

    coords = [(to_x(i), to_y(p.close)) for i, p in enumerate(points)]
    line = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
    area = (
        f"M {coords[0][0]:.2f},{bottom:.2f} "
        + " ".join(f"L {x:.2f},{y:.2f}" for x, y in coords)
        + f" L {coords[-1][0]:.2f},{bottom:.2f} Z"
    )

    # Y-Achse: runde Werte, hairline, sparsam. Der Endwert ist direkt
    # beschriftet - eine Achsenmarke auf gleicher Hoehe wuerde mit ihm
    # kollidieren und weicht deshalb.
    last_y = to_y(points[-1].close)
    y_ticks: list[Tick] = []
    value = axis_low
    while value <= axis_high + step / 2:
        position = to_y(value)
        if abs(position - last_y) >= LABEL_MIN_GAP:
            y_ticks.append(Tick(round(position, 2), _format_price(value)))
        value += step

    # X-Achse: ein Tick je Monatswechsel, hoechstens sechs
    month_starts = [
        i for i, p in enumerate(points)
        if i == 0 or p.date.month != points[i - 1].date.month
    ]
    if len(month_starts) > 6:
        keep = max(1, round(len(month_starts) / 6))
        month_starts = month_starts[::keep]
    x_ticks: list[Tick] = []
    for order, i in enumerate(month_starts):
        if order == 0:
            anchor = "start"
        elif order == len(month_starts) - 1:
            anchor = "end"
        else:
            anchor = "middle"
        x_ticks.append(Tick(round(to_x(i), 2), _format_date(points[i].date), anchor))

    index_by_date = {p.date: i for i, p in enumerate(points)}
    markers: list[Marker] = []
    for day, kind, label in events or ():
        index = index_by_date.get(day)
        if index is None:
            # Naechster vorhandener Handelstag am oder nach dem Ereignis.
            later = [i for i, p in enumerate(points) if p.date >= day]
            if not later:
                continue
            index = later[0]
        x, y = coords[index]
        markers.append(
            Marker(
                x=round(x, 2),
                y=round(y, 2),
                kind=kind,
                label=label,
                date=points[index].date.isoformat(),
                close=points[index].close,
            )
        )

    hover = tuple(
        HoverPoint(round(x, 2), round(y, 2), points[i].date.isoformat(), points[i].close)
        for i, (x, y) in enumerate(coords)
    )

    return PriceChart(
        line=line,
        area=area,
        markers=tuple(markers),
        y_ticks=tuple(y_ticks),
        x_ticks=tuple(x_ticks),
        hover=hover,
        last_x=round(coords[-1][0], 2),
        last_y=round(coords[-1][1], 2),
        last_label=_format_price(points[-1].close),
        empty=False,
    )
