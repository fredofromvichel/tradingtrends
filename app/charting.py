"""Geometrie des Kursverlaufs - reine Rechnung, kein Rendering.

Erzeugt aus einer Kursreihe die Koordinaten fuer ein Liniendiagramm mit
Ereignismarkern. Eine Achse; die Marker tragen ihre Identitaet ueber die Form
(Dreieck auf / Dreieck ab / offener Kreis), nicht allein ueber die Farbe.

Darunter liegen zwei Spuren mit derselben Zeitachse: wann PEAD und Momentum
long oder short waren. Sie stehen bewusst nicht auf der Kursflaeche - zwei
Strategien, die sich zeitlich ueberlappen, wuerden sich dort gegenseitig
zudecken. Gleitende Durchschnitte und die 52-Wochen-Spanne sind Bezugslinien
auf derselben Preisachse und werden nur bei Bedarf eingeblendet.

Frei von Datenbank- und Netzzugriffen (siehe tests/test_charting.py).
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from dataclasses import dataclass, field

from app.indicators import PricePoint

# Zeichenflaeche in Nutzer-Einheiten; das SVG skaliert per viewBox.
WIDTH = 720
HEIGHT = 272
PAD_LEFT = 8
PAD_RIGHT = 62      # Platz fuer das Endwert-Label und die Spurnamen
PAD_TOP = 14
PAD_BOTTOM = 70     # Spuren plus Datumsachse unter der Kursflaeche

# Spuren fuer die Signalphasen, unter der Kursflaeche. Balken <= 24 px hoch.
LANE_HEIGHT = 11
LANES = {"pead": 214, "momentum": 231}      # Oberkante je Spur
LANE_LABELS = {"pead": "PEAD", "momentum": "Momentum"}
AXIS_BASELINE = HEIGHT - 8
MIN_BAR_WIDTH = 3.0
SMA_WINDOWS = (50, 200)
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
    retro: bool = False


@dataclass(frozen=True)
class PhaseInput:
    """Eine Position, wie sie in die Spur soll - Quelle egal (live oder rueckwirkend)."""
    lane: str               # "pead" | "momentum"
    side: str               # "long" | "short"
    label: str              # BUY / SELL / LONG / SHORT
    retro: bool
    entry: dt.date
    exit: dt.date | None    # None: Haltedauer laeuft noch
    result: float | None    # realisiert, bei offener Position der laufende Stand
    benchmark: float | None
    note: str = ""          # "SUE +1,81", "Rang 2 von 22"


@dataclass(frozen=True)
class Phase:
    lane: str
    side: str
    label: str
    retro: bool
    open: bool
    x0: float
    x1: float
    y: float
    height: float
    entry: str
    exit: str | None
    result: float | None
    benchmark: float | None
    note: str
    # Einstieg liegt vor dem Diagrammbeginn - der Balken beginnt am Rand.
    clipped: bool = False


@dataclass(frozen=True)
class RefLine:
    """Bezugslinie auf der Preisachse (gleitender Durchschnitt)."""
    key: str                # "sma50" | "sma200"
    label: str              # "50 T"
    points: str
    label_x: float
    label_y: float


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
    sma50: float | None = None
    sma200: float | None = None


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
    phases: tuple[Phase, ...] = ()
    ref_lines: tuple[RefLine, ...] = ()
    # 52-Wochen-Spanne als waagrechte Bezugslinien.
    high_y: float | None = None
    low_y: float | None = None
    high_label: str = ""
    low_label: str = ""
    lane_label_x: float = WIDTH - PAD_RIGHT + 6
    lane_y: dict = field(default_factory=lambda: dict(LANES))
    lane_height: float = LANE_HEIGHT
    lane_names: dict = field(default_factory=lambda: dict(LANE_LABELS))
    axis_baseline: float = AXIS_BASELINE

    @property
    def hover_data(self) -> list[dict]:
        """Punkte fuer das Fadenkreuz, JSON-tauglich."""
        return [
            {"x": h.x, "y": h.y, "date": h.date, "close": h.close,
             "sma50": h.sma50, "sma200": h.sma200}
            for h in self.hover
        ]

    @property
    def phase_data(self) -> list[dict]:
        """Phasen fuer den Tooltip - welche Positionen an einem Tag liefen."""
        return [
            {"x0": p.x0, "x1": p.x1, "lane": p.lane, "label": p.label, "retro": p.retro,
             "open": p.open, "entry": p.entry, "exit": p.exit, "result": p.result,
             "benchmark": p.benchmark, "note": p.note}
            for p in self.phases
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


def rolling_sma(closes: list[float], window: int) -> list[float | None]:
    """Gleitender Durchschnitt je Position; None, solange die Reihe zu kurz ist."""
    out: list[float | None] = []
    total = 0.0
    for i, value in enumerate(closes):
        total += value
        if i >= window:
            total -= closes[i - window]
        out.append(total / window if i >= window - 1 else None)
    return out


def build_price_chart(
    points: list[PricePoint],
    events: list[tuple] | None = None,
    phases: list[PhaseInput] | None = None,
    history: list[PricePoint] | None = None,
) -> PriceChart:
    """Baut die Zeichengeometrie.

    ``events`` sind Tupel aus (Datum, Art, Beschriftung[, rueckwirkend]); die
    Art ist "BUY", "SELL" oder "NEUTRAL". Ereignisse ausserhalb der Kursreihe
    werden auf den naechstgelegenen vorhandenen Handelstag gelegt, damit ein
    Marker nie ausserhalb der Flaeche landet.

    ``history`` ist die laengere Reihe, deren Ende ``points`` bildet. Aus ihr
    kommen die gleitenden Durchschnitte - der 200-Tage-Schnitt am ersten
    Diagrammtag braucht 199 Kurse davor.
    """
    if len(points) < 2:
        return PriceChart()

    history = history if history and history[-1].date == points[-1].date else points
    offset = len(history) - len(points)
    sma = {}
    for window in SMA_WINDOWS:
        full = rolling_sma([p.close for p in history], window)
        sma[window] = full[offset:]

    closes = [p.close for p in points]
    # Die Achse muss auch die Durchschnitte fassen, sonst liefen sie aus dem Bild.
    reference = [v for window in SMA_WINDOWS for v in sma[window] if v is not None]
    low, high = min(closes + reference), max(closes + reference)
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

    dates = [p.date for p in points]
    index_by_date = {d: i for i, d in enumerate(dates)}
    markers: list[Marker] = []
    for event in events or ():
        day, kind, label = event[0], event[1], event[2]
        retro = bool(event[3]) if len(event) > 3 else False
        index = index_by_date.get(day)
        if index is None:
            # Naechster vorhandener Handelstag am oder nach dem Ereignis.
            index = bisect.bisect_left(dates, day)
            if index >= len(dates):
                continue
        x, y = coords[index]
        markers.append(
            Marker(
                x=round(x, 2),
                y=round(y, 2),
                kind=kind,
                label=label,
                date=points[index].date.isoformat(),
                close=points[index].close,
                retro=retro,
            )
        )

    chart_phases: list[Phase] = []
    for phase in phases or ():
        if phase.lane not in LANES:
            continue
        if phase.exit is not None and phase.exit < dates[0]:
            continue            # ganz vor dem sichtbaren Zeitraum
        if phase.entry > dates[-1]:
            continue
        clipped = phase.entry < dates[0]
        i0 = 0 if clipped else bisect.bisect_left(dates, phase.entry)
        i1 = len(dates) - 1 if phase.exit is None else bisect.bisect_right(dates, phase.exit) - 1
        i1 = max(i0, min(i1, len(dates) - 1))
        x0, x1 = to_x(i0), to_x(i1)
        if x1 - x0 < MIN_BAR_WIDTH:
            x1 = x0 + MIN_BAR_WIDTH
        chart_phases.append(
            Phase(
                lane=phase.lane,
                side=phase.side,
                label=phase.label,
                retro=phase.retro,
                open=phase.exit is None,
                x0=round(x0, 2),
                x1=round(x1, 2),
                y=LANES[phase.lane],
                height=LANE_HEIGHT,
                entry=phase.entry.isoformat(),
                exit=phase.exit.isoformat() if phase.exit else None,
                result=phase.result,
                benchmark=phase.benchmark,
                note=phase.note,
                clipped=clipped,
            )
        )

    ref_lines: list[RefLine] = []
    # Beschriftung versetzt im ersten Drittel: rechts stehen Endwert und Achse,
    # links oben die 52-Wochen-Spanne - und gestaffelt, damit sich die beiden
    # Durchschnitte nicht gegenseitig verdecken.
    label_at = {200: 0.16, 50: 0.30}
    for window in SMA_WINDOWS:
        values = sma[window]
        pts = [(to_x(i), to_y(v)) for i, v in enumerate(values) if v is not None]
        if len(pts) < 2:
            continue
        anchor = pts[min(len(pts) - 1, int(len(pts) * label_at.get(window, 0.2)))]
        ref_lines.append(
            RefLine(
                key=f"sma{window}",
                label=f"{window} T",
                points=" ".join(f"{x:.2f},{y:.2f}" for x, y in pts),
                label_x=round(anchor[0], 2),
                label_y=round(max(top + 9, anchor[1] - 6), 2),
            )
        )

    hover = tuple(
        HoverPoint(
            round(x, 2), round(y, 2), points[i].date.isoformat(), points[i].close,
            sma50=sma[50][i], sma200=sma[200][i],
        )
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
        phases=tuple(chart_phases),
        ref_lines=tuple(ref_lines),
        high_y=round(to_y(max(closes)), 2),
        low_y=round(to_y(min(closes)), 2),
        high_label=f"Hoch {_format_price(max(closes))}",
        low_label=f"Tief {_format_price(min(closes))}",
    )
