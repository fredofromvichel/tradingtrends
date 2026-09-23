"""Die beobachtete Titelliste - gepflegt in der Oberflaeche, gespeichert in der DB.

Bis zu dieser Version war config.yaml die Quelle, und jeder Lauf glich die
Datenbank daran an. Das hatte zwei Folgen: Aenderungen brauchten Shell-Zugriff,
und jedes ``git pull`` brachte die Platzhalterliste aus dem Repository zurueck -
wer dann neu startete, hatte seine Titel deaktiviert.

Jetzt gilt: Die Datenbank ist die Quelle. config.yaml liefert nur die
Startliste fuer eine leere Datenbank, also fuer eine frische Installation
oder nach ``make reset``.

Entfernt wird nie hart, sondern ueber ``active=False``. Signale verweisen per
Fremdschluessel auf den Titel, und die Historie soll lesbar bleiben.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MomentumSignal, Signal, Ticker
from app.signals import OPEN
from app.sources.yahoo_earnings import is_us_symbol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedResult:
    seeded: bool
    # Symbole aus config.yaml, die nicht (mehr) beobachtet werden. Nur fuer
    # die Logzeile: wer die Datei bearbeitet, soll erfahren, dass sie nicht
    # mehr wirkt.
    ignored: tuple[str, ...] = ()


@dataclass
class Removal:
    symbol: str
    # Positionen laufen bis zu ihrem Ausstieg weiter - sonst bliebe die
    # Statistik mit halben Trades zurueck.
    open_pead: int = 0
    open_momentum: int = 0
    last_exit_hint: dt.date | None = None
    was_active: bool = True

    @property
    def has_open_positions(self) -> bool:
        return bool(self.open_pead or self.open_momentum)


@dataclass
class Addition:
    added: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[str]:
        return self.added + self.reactivated


def seed_tickers(session: Session, symbols: tuple[str, ...]) -> SeedResult:
    """Uebernimmt die Startliste aus config.yaml - aber nur in eine leere DB."""
    known = session.scalar(select(func.count()).select_from(Ticker)) or 0
    if known == 0:
        for symbol in dict.fromkeys(symbols):
            session.add(Ticker(symbol=symbol, active=True))
        if symbols:
            log.info("Startliste aus config.yaml uebernommen: %s", ", ".join(symbols))
        return SeedResult(seeded=bool(symbols))

    active = set(active_symbols(session))
    ignored = tuple(s for s in symbols if s not in active)
    return SeedResult(seeded=False, ignored=ignored)


def active_symbols(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(Ticker.symbol).where(Ticker.active.is_(True)).order_by(Ticker.symbol)
        ).all()
    )


def inactive_tickers(session: Session) -> list[Ticker]:
    return list(
        session.scalars(
            select(Ticker).where(Ticker.active.is_(False)).order_by(Ticker.symbol)
        ).all()
    )


def add(session: Session, symbols: list[str], names: dict[str, str] | None = None) -> Addition:
    """Nimmt Symbole auf oder reaktiviert sie.

    ``names`` stammt aus der Symbolpruefung und wird nur fuer Titel ausserhalb
    der USA uebernommen. Bei US-Titeln holt der naechste Lauf das Finnhub-
    Profil mit Name, Branche und Boerse - aber nur, solange noch kein Name
    gesetzt ist. Ein vorab eingetragener Yahoo-Name verhinderte das, und die
    Branche fehlte dauerhaft (die Recherche-Links brauchen sie). Ausserhalb
    der USA sperrt der freie Tarif das Profil ohnehin; dort ist der
    Yahoo-Name besser als keiner.
    """
    names = {s: n for s, n in (names or {}).items() if not is_us_symbol(s)}
    result = Addition()
    for symbol in dict.fromkeys(symbols):
        row = session.get(Ticker, symbol)
        if row is None:
            session.add(Ticker(symbol=symbol, active=True, name=names.get(symbol)))
            result.added.append(symbol)
            log.info("Titel aufgenommen: %s", symbol)
        elif not row.active:
            row.active = True
            if row.name is None and names.get(symbol):
                row.name = names[symbol]
            result.reactivated.append(symbol)
            log.info("Titel wieder aufgenommen: %s", symbol)
        else:
            result.already.append(symbol)
    return result


def remove(session: Session, symbol: str) -> Removal | None:
    """Beendet die Beobachtung. Offene Positionen laufen bis zum Ausstieg weiter."""
    row = session.get(Ticker, symbol)
    if row is None:
        return None

    removal = Removal(symbol=symbol, was_active=row.active)
    removal.open_pead = session.scalar(
        select(func.count()).select_from(Signal)
        .where(Signal.symbol == symbol, Signal.status == OPEN)
    ) or 0
    removal.open_momentum = session.scalar(
        select(func.count()).select_from(MomentumSignal)
        .where(MomentumSignal.symbol == symbol, MomentumSignal.status == OPEN)
    ) or 0

    if row.active:
        row.active = False
        log.info(
            "Titel nicht mehr beobachtet: %s (offene Positionen: %d PEAD, %d Momentum)",
            symbol, removal.open_pead, removal.open_momentum,
        )
    return removal


def config_block(session: Session) -> str:
    """Die aktuelle Liste als tickers-Block fuer config.yaml.

    Die einzige Stelle, an der die Liste die Datenbank verlaesst: als
    Sicherung vor ``make reset`` oder fuer eine zweite Installation.
    """
    rows = session.scalars(
        select(Ticker).where(Ticker.active.is_(True)).order_by(Ticker.symbol)
    ).all()
    if not rows:
        return "tickers: []\n"
    width = max(len(r.symbol) for r in rows)
    lines = ["tickers:"]
    for r in rows:
        comment = f"  # {r.name}" if r.name else ""
        lines.append(f"  - {r.symbol.ljust(width)}{comment}".rstrip())
    return "\n".join(lines) + "\n"


def set_name(session: Session, symbol: str, name: str | None) -> Ticker | None:
    """Traegt einen Namen von Hand ein - oder gibt ihn frei (leer).

    Ein Handeintrag wird von keinem Abruf ueberschrieben. Leer heisst: wieder
    automatisch, und der naechste Lauf fragt die Quellen erneut.
    """
    row = session.get(Ticker, symbol)
    if row is None:
        return None
    name = " ".join((name or "").split())[:128]
    if name:
        row.name = name
        row.name_manual = True
    else:
        row.name = None
        row.name_manual = False
        row.profile_checked_at = None
    return row
