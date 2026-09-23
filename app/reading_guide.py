"""Lesehilfe: einen Kursverlauf selbst einordnen.

Jede Kennzahl kommt mit drei Dingen: dem heutigen Wert, einem Satz, wie er
fuer diesen Titel zu lesen ist, und der Beleglage - wie gut die Forschung
stuetzt, dass die Kennzahl etwas ueber die Zukunft sagt. Die Beleglage ist
der eigentliche Lerngehalt: Die bekanntesten Chartsignale sind oft die am
schwaechsten belegten.

Nichts hier ist eine Empfehlung. Die Lesarten beschreiben, was ein Wert
bedeutet, nicht was zu tun ist.

Frei von Datenbankzugriffen (siehe tests/test_reading_guide.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.indicators import PriceContext
from app.momentum import group_size


@dataclass(frozen=True)
class Evidence:
    level: int          # 0 = rein beschreibend ... 4 = gut belegt
    label: str
    text: str
    sources: str = ""


EVIDENCE = {
    "momentum": Evidence(
        4, "gut belegt",
        "Einer der am besten dokumentierten Effekte: Aktien, die über zwölf Monate "
        "besser liefen als andere, taten das im Schnitt auch in den folgenden "
        "Monaten – in vielen Ländern und Anlageklassen. Zwei Einschränkungen: "
        "gemessen über Hunderte Aktien, nicht über zwanzig, und mit gelegentlichen "
        "heftigen Einbrüchen, wenn der Markt nach einem Absturz dreht.",
        "Jegadeesh & Titman (1993); Asness, Moskowitz & Pedersen (2013); "
        "Daniel & Moskowitz (2016)",
    ),
    "high52": Evidence(
        3, "belegt",
        "Aktien nahe ihrem 52-Wochen-Hoch entwickelten sich in Studien im Schnitt "
        "besser als solche weit darunter. Eine Erklärung: Anleger orientieren sich "
        "am alten Hoch und reagieren zu zögerlich auf gute Nachrichten.",
        "George & Hwang (2004)",
    ),
    "risk": Evidence(
        3, "gut belegt als Risikomaß",
        "Als Maß für das Risiko unstrittig. Als Renditesignal eher umgekehrt als "
        "vermutet: Stark schwankende Einzeltitel brachten im Schnitt keine höhere, "
        "eher eine niedrigere Rendite.",
        "Ang, Hodrick, Xing & Zhang (2006); Baker, Bradley & Wurgler (2011)",
    ),
    "reversal": Evidence(
        2, "belegt, bei großen Titeln schwach",
        "Auf einen sehr starken oder sehr schwachen Monat folgt bei Einzelaktien "
        "im Schnitt eine leichte Gegenbewegung. Der Effekt sitzt vor allem in "
        "kleinen, wenig gehandelten Titeln; bei großen ist er nach Kosten kaum "
        "nutzbar. Er ist der Grund, warum die Momentum-Regel den letzten Monat "
        "auslässt.",
        "Jegadeesh (1990); Lehmann (1990); Avramov, Chordia & Goyal (2006)",
    ),
    "trend200": Evidence(
        2, "gemischt",
        "Als Beschreibung des Trends nützlich. Als Handelsregel (über der Linie "
        "kaufen, darunter verkaufen) fanden frühe Studien Vorteile beim "
        "Dow-Jones-Index; spätere Prüfungen zeigten, dass viel davon auf die "
        "nachträgliche Auswahl der Regel zurückgeht und in jüngeren Daten fehlt.",
        "Brock, Lakonishok & LeBaron (1992); Sullivan, Timmermann & White (1999)",
    ),
    "cross": Evidence(
        1, "schwach",
        "Populär, aber als Signal schwach: Bis sich die Linien kreuzen, ist ein "
        "großer Teil der Bewegung vorbei, und nach Kosten schneiden solche "
        "Kreuzungsregeln bei Einzelaktien in Prüfungen kaum besser ab als Zufall.",
        "Sullivan, Timmermann & White (1999)",
    ),
    "volume": Evidence(
        0, "beschreibend",
        "Zeigt, ob gerade ungewöhnlich viel gehandelt wird – also ob etwas "
        "verarbeitet wird. Über die Richtung sagt es allein nichts.",
    ),
}


# Wind statt Befehl: beschreibt, ob eine Kennzahl fuer steigende oder fallende
# Kurse spricht - nicht, was zu tun ist.
TAILWIND = "rueckenwind"
HEADWIND = "gegenwind"
CALM = "flaute"
WIND_LABELS = {TAILWIND: "Rückenwind", HEADWIND: "Gegenwind", CALM: "Flaute"}


@dataclass(frozen=True)
class Reading:
    key: str
    question: str
    value: str
    reading: str
    evidence: Evidence
    # Ebene im Kursdiagramm, die dazu passt ("im Chart zeigen").
    layer: str | None = None
    # None: die Kennzahl sagt nichts ueber die Richtung (Risiko, Umsatz).
    wind: str | None = None

    @property
    def wind_label(self) -> str:
        return WIND_LABELS.get(self.wind, "keine Richtung")


@dataclass(frozen=True)
class Outlook:
    """Gesamtlage: die Windrichtungen, gewichtet nach Beleglage."""
    wind: str               # rueckenwind | gegenwind | flaute
    label: str
    score: float            # -1 ... +1
    tailwind: int
    headwind: int
    calm: int

    @property
    def counted(self) -> int:
        return self.tailwind + self.headwind + self.calm


# Ab diesem gewichteten Saldo gilt die Lage als eindeutig.
OUTLOOK_THRESHOLD = 0.35


def outlook(readings: list[Reading]) -> Outlook | None:
    """Fasst die gerichteten Kennzahlen zusammen - Belastbares zaehlt mehr.

    Gewicht = Beleglage (gut belegt 4 ... schwach 1). Eine schwach belegte
    Linienkreuzung kann so eine gut belegte relative Staerke nicht aufwiegen.
    """
    sign = {TAILWIND: 1, HEADWIND: -1, CALM: 0}
    gerichtet = [r for r in readings if r.wind is not None and r.evidence.level > 0]
    if not gerichtet:
        return None
    total = sum(r.evidence.level for r in gerichtet)
    score = sum(sign[r.wind] * r.evidence.level for r in gerichtet) / total
    if score >= OUTLOOK_THRESHOLD:
        wind, label = TAILWIND, "Überwiegend Rückenwind"
    elif score <= -OUTLOOK_THRESHOLD:
        wind, label = HEADWIND, "Überwiegend Gegenwind"
    else:
        wind, label = CALM, "Gemischte Lage"
    return Outlook(
        wind=wind, label=label, score=round(score, 2),
        tailwind=sum(1 for r in gerichtet if r.wind == TAILWIND),
        headwind=sum(1 for r in gerichtet if r.wind == HEADWIND),
        calm=sum(1 for r in gerichtet if r.wind == CALM),
    )


def _pct(value: float | None, digits: int = 1, sign: bool = True) -> str:
    if value is None:
        return "–"
    text = f"{value * 100:{'+' if sign else ''}.{digits}f} %"
    return text.replace(".", ",").replace("-", "−")


def _trend(ctx: PriceContext) -> Reading:
    q = "Zeigt der langfristige Trend nach oben?"
    d = ctx.distance_to(ctx.sma200)
    if d is None:
        return Reading("trend200", q, "–", "Noch keine 200 Handelstage Kurshistorie.",
                       EVIDENCE["trend200"], "sma200")
    slope = ctx.sma200_change_20d
    wind = CALM if abs(d) < 0.02 else (TAILWIND if d > 0 else HEADWIND)
    if abs(d) < 0.02:
        text = "Der Kurs liegt fast auf der 200-Tage-Linie – kein eindeutiger Trend."
    elif d > 0:
        text = "Der Kurs liegt über dem Schnitt der letzten 200 Handelstage: Der langfristige Trend zeigt nach oben."
        if slope is not None and slope < -0.01:
            text += " Die Linie selbst fällt aber noch – der Anstieg ist jung."
        elif slope is not None and slope > 0.01:
            text += " Die Linie selbst steigt ebenfalls."
    else:
        text = "Der Kurs liegt unter dem Schnitt der letzten 200 Handelstage: Der langfristige Trend zeigt nach unten."
        if slope is not None and slope > 0.01:
            text += " Die Linie selbst steigt aber noch – der Rückgang ist jung."
        elif slope is not None and slope < -0.01:
            text += " Die Linie selbst fällt ebenfalls."
    return Reading("trend200", q, f"{_pct(d)} zur 200-Tage-Linie", text,
                   EVIDENCE["trend200"], "sma200", wind)


def _cross(ctx: PriceContext) -> Reading:
    q = "Liegt der kurze Schnitt über dem langen?"
    if ctx.sma50 is None or ctx.sma200 is None:
        return Reading("cross", q, "–", "Zu kurze Kurshistorie.", EVIDENCE["cross"], "sma50")
    wind = TAILWIND if ctx.sma50 >= ctx.sma200 else HEADWIND
    if ctx.sma50 >= ctx.sma200:
        value = "50 T über 200 T"
        text = ("Der Schnitt der letzten 50 Tage liegt über dem der letzten 200 – im "
                "Jargon nach dem Kreuzen ein „Golden Cross“. Das bestätigt nur, was der "
                "Kurs schon getan hat.")
    else:
        value = "50 T unter 200 T"
        text = ("Der Schnitt der letzten 50 Tage liegt unter dem der letzten 200 – im "
                "Jargon nach dem Kreuzen ein „Death Cross“. Das bestätigt nur, was der "
                "Kurs schon getan hat.")
    return Reading("cross", q, value, text, EVIDENCE["cross"], "sma50", wind)


def _momentum(rank: int | None, universe: int | None, score: float | None,
              fraction: float) -> Reading:
    q = "Lief der Titel besser als deine übrigen?"
    if rank is None or not universe:
        return Reading("momentum", q, "–",
                       "Zu kurze Kurshistorie für eine Rangfolge (nötig: gut ein Jahr).",
                       EVIDENCE["momentum"], "momentum")
    g = group_size(universe, fraction) if universe >= 2 else 1
    if rank <= g:
        where, wind = f"In der Spitzengruppe (die besten {g}): Die Momentum-Regel wäre long.", TAILWIND
    elif rank > universe - g:
        where, wind = f"In der Schlussgruppe (die schwächsten {g}): Die Momentum-Regel wäre short.", HEADWIND
    else:
        where, wind = "Im Mittelfeld: Die Momentum-Regel hält sich heraus.", CALM
    text = (f"{where} Gemessen an der Kursentwicklung über zwölf Monate ohne den letzten "
            f"({_pct(score, 0)}). Relativ gemeint: besser oder schlechter als deine "
            "übrigen Titel, nicht gut oder schlecht an sich.")
    return Reading("momentum", q, f"Rang {rank} von {universe}", text,
                   EVIDENCE["momentum"], "momentum", wind)


def _high(ctx: PriceContext) -> Reading:
    q = "Wie weit ist der Kurs vom Jahreshoch entfernt?"
    d = ctx.distance_to_high
    if d is None:
        return Reading("high52", q, "–", "Zu kurze Kurshistorie.", EVIDENCE["high52"], "range")
    if d >= -0.05:
        text, wind = "Nahe am Jahreshoch.", TAILWIND
    elif d <= -0.30:
        text, wind = ("Weit unter dem Jahreshoch – der Titel hat im letzten Jahr viel "
                      "Vertrauen verloren."), HEADWIND
    else:
        text, wind = "Zwischen Jahreshoch und -tief.", CALM
    if ctx.range_position is not None:
        text += f" Er steht bei {ctx.range_position * 100:.0f} % der Jahresspanne (0 % = Tief, 100 % = Hoch)."
    return Reading("high52", q, f"{_pct(d)} zum 52-Wochen-Hoch", text,
                   EVIDENCE["high52"], "range", wind)


def _reversal(ctx: PriceContext) -> Reading:
    q = "War der letzte Monat außergewöhnlich?"
    r = ctx.return_1m
    if r is None:
        return Reading("reversal", q, "–", "Zu kurze Kurshistorie.", EVIDENCE["reversal"])
    if r > 0.10:
        wind = HEADWIND
        text = ("Ein sehr starker Monat. Auf solche Sprünge folgt bei Einzelaktien im "
                "Schnitt eher eine leichte Gegenbewegung.")
    elif r < -0.10:
        wind = TAILWIND
        text = ("Ein sehr schwacher Monat. Auf solche Rückgänge folgt bei Einzelaktien im "
                "Schnitt eher eine leichte Erholung – kein Kaufsignal, der Effekt ist klein.")
    else:
        wind = CALM
        text = "Ein unauffälliger Monat."
    return Reading("reversal", q, f"{_pct(r)} in 21 Handelstagen", text,
                   EVIDENCE["reversal"], None, wind)


def _risk(ctx: PriceContext) -> Reading:
    q = "Wie viel Risiko steckt im Titel?"
    vol = ctx.volatility_annualised
    if vol is None:
        return Reading("risk", q, "–", "Zu kurze Kurshistorie.", EVIDENCE["risk"])
    text = ("Schwankungen in dieser Größenordnung sind für diesen Titel normal. Sie "
            "sagen nichts über die Richtung, aber viel über die Positionsgröße: Je "
            "stärker ein Titel schwankt, desto kleiner sollte der Anteil sein.")
    if vol > 0.40:
        text += " Zum Vergleich: Ein breiter Aktienindex schwankt typischerweise um 15 bis 20 % im Jahr."
    value = f"{vol * 100:.0f} % im Jahr"
    if ctx.max_drawdown_52w is not None:
        value += f" · größter Rückgang {_pct(ctx.max_drawdown_52w, 0)}"
    return Reading("risk", q, value, text, EVIDENCE["risk"])


def _volume(ctx: PriceContext) -> Reading:
    q = "Wird gerade ungewöhnlich viel gehandelt?"
    ratio = ctx.volume_ratio
    if ratio is None:
        return Reading("volume", q, "–", "Kein Umsatz gemeldet.", EVIDENCE["volume"])
    if ratio >= 2:
        text = ("Deutlich mehr Umsatz als üblich – es wird gerade etwas verarbeitet. Die "
                "Recherche-Links weiter unten zeigen, was.")
    else:
        text = "Normaler Umsatz."
    value = f"{ratio:.1f}× des 20-Tage-Schnitts".replace(".", ",")
    return Reading("volume", q, value, text, EVIDENCE["volume"])


def build(
    ctx: PriceContext,
    rank: int | None = None,
    universe: int | None = None,
    score: float | None = None,
    group_fraction: float = 0.3,
) -> list[Reading]:
    """Die Lesehilfe fuer einen Titel, nach Beleglage sortiert - Belastbares zuerst."""
    readings = [
        _momentum(rank, universe, score, group_fraction),
        _high(ctx),
        _risk(ctx),
        _reversal(ctx),
        _trend(ctx),
        _cross(ctx),
        _volume(ctx),
    ]
    return sorted(readings, key=lambda r: -r.evidence.level)
