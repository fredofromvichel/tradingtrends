"""Cross-Sectional-Momentum als zweite, getrennte Signalquelle.

Die klassische Formulierung (Jegadeesh/Titman): Titel nach ihrer Rendite der
letzten zwoelf Monate rangieren, den juengsten Monat dabei **auslassen**, dann
die Spitzengruppe kaufen und die Schlussgruppe leerverkaufen. Der ausgelassene
Monat ist kein Schoenheitsfehler, sondern Absicht - auf sehr kurze Sicht neigen
Kurse zur Umkehr, was den Effekt sonst auffrisst.

Zwei Eigenschaften, die den Unterschied zu PEAD ausmachen:

* **Relativ, nicht absolut.** Ein Titel ist nicht "gut", sondern besser als die
  anderen im Universum. In einem fallenden Markt kann die Long-Gruppe komplett
  aus Verlierern bestehen, die nur weniger verloren haben.
* **Kalendergetrieben.** Umgeschichtet wird nach Plan, nicht als Reaktion auf
  ein Ereignis.

Frei von Datenbank- und Netzzugriffen (siehe tests/test_momentum.py).
"""

from __future__ import annotations

from dataclasses import dataclass

LONG = "LONG"
SHORT = "SHORT"

# Unterhalb dieser Universumsgroesse ist eine Rangfolge nicht sinnvoll: mit
# fuenf Titeln besteht jede Gruppe aus ein bis zwei Namen und das Ergebnis
# haengt an einem einzigen Wert.
MIN_UNIVERSE = 8


@dataclass(frozen=True)
class Ranked:
    symbol: str
    score: float
    rank: int          # 1 = staerkster Titel
    direction: str | None   # LONG | SHORT | None (Mittelfeld)


@dataclass(frozen=True)
class Basket:
    longs: tuple[Ranked, ...]
    shorts: tuple[Ranked, ...]
    middle: tuple[Ranked, ...]
    universe_size: int
    group_size: int
    reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.longs and not self.shorts

    @property
    def all_ranked(self) -> tuple[Ranked, ...]:
        return self.longs + self.middle + self.shorts


def momentum_score(
    closes: list[float], lookback_days: int = 252, skip_days: int = 21
) -> float | None:
    """Rendite ueber das Formationsfenster, ohne die juengsten ``skip_days``.

    ``closes`` ist aufsteigend sortiert. Das Ergebnis ist ein Bruch
    (0.25 == +25 %). Reicht die Historie nicht, wird None zurueckgegeben -
    ein kuerzeres Fenster stillschweigend zu verwenden waere eine andere
    Kennzahl unter gleichem Namen.
    """
    if lookback_days < 2:
        raise ValueError("lookback_days muss mindestens 2 sein.")
    if skip_days < 0:
        raise ValueError("skip_days darf nicht negativ sein.")
    if skip_days >= lookback_days:
        raise ValueError("skip_days muss kleiner als lookback_days sein.")

    if len(closes) < lookback_days + 1:
        return None

    start = closes[-(lookback_days + 1)]
    end = closes[-(skip_days + 1)]
    if start <= 0:
        return None
    return (end - start) / start


def required_history(lookback_days: int = 252) -> int:
    """Wie viele Kurstage fuer eine Bewertung noetig sind."""
    return lookback_days + 1


def group_size(universe_size: int, fraction: float) -> int:
    """Groesse der Long- bzw. Short-Gruppe.

    Mindestens einer, hoechstens so viele, dass sich Spitze und Schluss nicht
    ueberschneiden.
    """
    if not 0 < fraction < 0.5:
        raise ValueError("fraction muss zwischen 0 und 0.5 liegen.")
    size = max(1, round(universe_size * fraction))
    return min(size, universe_size // 2)


def rank_universe(
    scores: dict[str, float | None],
    fraction: float = 0.3,
    min_universe: int = MIN_UNIVERSE,
) -> Basket:
    """Bildet aus den Momentum-Werten die Long- und Short-Gruppe.

    Titel ohne Wert (zu kurze Historie) fallen aus dem Universum - sie
    unterschlagen wuerde die Rangfolge verzerren. Gleichstand wird alphabetisch
    aufgeloest, damit derselbe Datenstand dieselbe Gruppe ergibt.
    """
    usable = {s: v for s, v in scores.items() if v is not None}
    universe_size = len(usable)

    if universe_size < min_universe:
        return Basket(
            (), (), (), universe_size, 0,
            reason=(
                f"Nur {universe_size} von {len(scores)} Titeln haben genug "
                f"Kurshistorie; für eine Rangfolge sind mindestens "
                f"{min_universe} nötig."
            ),
        )

    ordered = sorted(usable.items(), key=lambda kv: (-kv[1], kv[0]))
    size = group_size(universe_size, fraction)

    ranked = [
        Ranked(symbol, score, index + 1, None)
        for index, (symbol, score) in enumerate(ordered)
    ]
    longs = tuple(
        Ranked(r.symbol, r.score, r.rank, LONG) for r in ranked[:size]
    )
    shorts = tuple(
        Ranked(r.symbol, r.score, r.rank, SHORT) for r in ranked[-size:]
    )
    middle = tuple(ranked[size: universe_size - size])

    return Basket(
        longs=longs,
        shorts=shorts,
        middle=middle,
        universe_size=universe_size,
        group_size=size,
        reason=f"{universe_size} Titel rangiert, je {size} in Spitze und Schluss.",
    )
