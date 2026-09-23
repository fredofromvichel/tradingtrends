"""Aufbereitung der Titelverwaltung fuer die Oberflaeche.

Rueckmeldungen nach einer Aenderung kommen als Query-Parameter der
Weiterleitung (Post/Redirect/Get) - ohne Sitzung und ohne Cookie. Angezeigt
wird davon nur, was sich als Symbol lesen laesst.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ticker
from app.symbols import Lookup, normalize_symbol


@dataclass(frozen=True)
class Flash:
    kind: str                  # ok | warn
    text: str
    undo: str | None = None    # Symbol, das per Knopf wieder aufgenommen werden kann


def _symbols(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in (raw or "").split(","):
        symbol = normalize_symbol(part)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _liste(symbols: list[str]) -> str:
    if len(symbols) == 1:
        return symbols[0]
    return ", ".join(symbols[:-1]) + " und " + symbols[-1]


def flash_from_query(params: Mapping[str, str]) -> list[Flash]:
    out: list[Flash] = []
    added = _symbols(params.get("aufgenommen"))
    back = _symbols(params.get("reaktiviert"))
    already = _symbols(params.get("bereits"))
    removed = _symbols(params.get("entfernt"))

    if added or back:
        teile = []
        if added:
            teile.append(f"{_liste(added)} aufgenommen")
        if back:
            teile.append(f"{_liste(back)} wieder aufgenommen, bisherige Historie bleibt")
        out.append(Flash("ok", "; ".join(teile) + ". Kurse und Termine werden im "
                         "Hintergrund geladen – das dauert ein bis zwei Minuten."))
    if already:
        out.append(Flash("warn", f"{_liste(already)} wird bereits beobachtet."))
    for symbol in removed[:1]:
        text = f"{symbol} wird nicht mehr beobachtet. Die Historie bleibt erhalten."
        try:
            offen = int(params.get("offen") or 0)
        except ValueError:
            offen = 0
        if offen:
            text += (f" {offen} offene Position{'en laufen' if offen > 1 else ' läuft'} "
                     "bis zum regulären Ausstieg weiter; neue Signale entstehen nicht.")
        out.append(Flash("ok", text, undo=symbol))
    if params.get("nichts"):
        out.append(Flash("warn", "Nichts ausgewählt – bei mehrdeutigen Eingaben ist "
                         "bewusst nichts vorausgewählt."))
    return out


def mark_known(session: Session, lookups: list[Lookup]) -> None:
    """Markiert Listings, die schon (oder frueher) beobachtet werden."""
    symbols = {l.symbol for lk in lookups for c in lk.companies for l in c.listings}
    if not symbols:
        return
    state = dict(
        session.execute(
            select(Ticker.symbol, Ticker.active).where(Ticker.symbol.in_(symbols))
        ).all()
    )
    for lk in lookups:
        for company in lk.companies:
            for listing in company.listings:
                if listing.symbol in state:
                    listing.known = "active" if state[listing.symbol] else "inactive"
        pre = lk.preselected
        active = {l.symbol for c in lk.companies for l in c.listings if l.known == "active"}
        lk.choice = pre if pre and pre not in active else None
