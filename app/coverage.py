"""Gedaechtnis fuer Tarif-Sperren bei Finnhub.

Ein 403 heisst: dieser Endpunkt ist fuer diesen Tarif nicht freigeschaltet.
Das aendert sich nicht bis zum naechsten Lauf und auch nicht bis naechste
Woche. Ohne Gedaechtnis wiederholte die Pipeline die aussichtslosen Anfragen
taeglich - bei einem gemischten Universum schnell ein paar Dutzend pro Lauf,
und die Meldung erschlaegt die Oberflaeche.

Gemerkt wird je (Symbol, Endpunkt). In groesseren Abstaenden wird einmal neu
probiert, damit ein Tarif-Upgrade von selbst auffaellt statt unbemerkt zu
bleiben.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FinnhubCoverage

log = logging.getLogger(__name__)

# Sprechende Namen fuer die Anzeige.
ENDPOINT_LABELS = {
    "/stock/profile2": "Stammdaten",
    "/calendar/earnings": "Earnings-Kalender",
    "/stock/earnings": "Earnings-Historie",
    "/stock/recommendation": "Analystenbild",
}


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def is_due_for_recheck(
    last_checked: dt.datetime, recheck_days: int, now: dt.datetime | None = None
) -> bool:
    """Ist die Sperre alt genug, um sie erneut zu probieren?

    Reine Entscheidung ohne Datenbankzugriff - so laesst sie sich pruefen.
    """
    if recheck_days < 1:
        raise ValueError("recheck_days muss mindestens 1 sein.")
    now = now or dt.datetime.now(dt.timezone.utc)
    return _as_utc(last_checked) <= now - dt.timedelta(days=recheck_days)


def should_skip(
    session: Session, symbol: str, endpoint: str, recheck_days: int
) -> bool:
    """True, wenn der Aufruf gespart werden kann."""
    row = session.scalar(
        select(FinnhubCoverage).where(
            FinnhubCoverage.symbol == symbol, FinnhubCoverage.endpoint == endpoint
        )
    )
    if row is None:
        return False
    if is_due_for_recheck(row.last_checked, recheck_days):
        # Wiedervorlage: einmal probieren. Der Zeitstempel wandert schon jetzt
        # weiter, damit ein erneuter 403 nicht sofort wieder faellig wird.
        row.last_checked = dt.datetime.now(dt.timezone.utc)
        row.attempts += 1
        log.info("Erneuter Versuch fuer gesperrten Endpunkt: %s %s", symbol, endpoint)
        return False
    return True


def record_block(session: Session, symbol: str, endpoint: str) -> None:
    """Merkt einen 403 fuer dieses Symbol und diesen Endpunkt."""
    now = dt.datetime.now(dt.timezone.utc)
    row = session.scalar(
        select(FinnhubCoverage).where(
            FinnhubCoverage.symbol == symbol, FinnhubCoverage.endpoint == endpoint
        )
    )
    if row is None:
        session.add(
            FinnhubCoverage(
                symbol=symbol, endpoint=endpoint, blocked_since=now, last_checked=now
            )
        )
    else:
        row.last_checked = now


def record_success(session: Session, symbol: str, endpoint: str) -> None:
    """Hebt eine Sperre auf - etwa nach einem Tarif-Upgrade."""
    row = session.scalar(
        select(FinnhubCoverage).where(
            FinnhubCoverage.symbol == symbol, FinnhubCoverage.endpoint == endpoint
        )
    )
    if row is not None:
        session.delete(row)
        log.info("Sperre aufgehoben: %s %s liefert wieder Daten.", symbol, endpoint)


def blocked_symbols(session: Session) -> list[str]:
    """Symbole, fuer die mindestens ein Endpunkt gesperrt ist."""
    return sorted(
        {row for (row,) in session.execute(select(FinnhubCoverage.symbol)).all()}
    )


def coverage_note(session: Session) -> str | None:
    """Ein Satz statt zwoelf Wiederholungen derselben Meldung."""
    symbols = blocked_symbols(session)
    if not symbols:
        return None
    endpoints = sorted(
        {row for (row,) in session.execute(select(FinnhubCoverage.endpoint)).all()}
    )
    labels = ", ".join(ENDPOINT_LABELS.get(e, e) for e in endpoints)
    return (
        f"{len(symbols)} Titel deckt der Finnhub-Tarif nicht ab ({labels}): "
        f"{', '.join(symbols)}. Für sie entstehen dauerhaft keine PEAD-Signale; "
        "Kurse und Momentum laufen normal weiter. Die Abfragen werden "
        "übersprungen und in größeren Abständen erneut geprüft."
    )
