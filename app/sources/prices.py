"""Kursdaten via yfinance (Yahoo Finance).

Ein Batch-Download fuer alle Ticker, mit Einzelabruf als Rueckfallebene -
Yahoo laesst gelegentlich einzelne Symbole in einem Batch aus.
Kurse werden split- und dividendenbereinigt geladen (``auto_adjust=True``),
damit die Rendite ueber die Haltedauer nicht durch einen Aktiensplit verzerrt
wird.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceRow:
    symbol: str
    date: dt.date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: int | None


def fetch_prices(
    symbols: list[str], start: dt.date, end: dt.date
) -> tuple[list[PriceRow], list[str]]:
    """Laedt Tageskurse fuer ``symbols`` im Fenster [start, end].

    Rueckgabe: (Kurszeilen, Liste der Symbole ohne Daten).
    """
    if not symbols:
        return [], []

    # yfinance behandelt 'end' exklusiv.
    end_exclusive = end + dt.timedelta(days=1)
    rows: list[PriceRow] = []
    missing: list[str] = []

    frame = _download(symbols, start, end_exclusive)
    for symbol in symbols:
        sub = _extract(frame, symbol, single=len(symbols) == 1)
        if sub is None or sub.empty:
            missing.append(symbol)
            continue
        rows.extend(_to_rows(symbol, sub))

    # Einzelabruf fuer alles, was der Batch nicht geliefert hat.
    still_missing: list[str] = []
    for symbol in missing:
        log.info("Kurse fuer %s fehlen im Batch - Einzelabruf.", symbol)
        frame_single = _download([symbol], start, end_exclusive)
        sub = _extract(frame_single, symbol, single=True)
        if sub is None or sub.empty:
            still_missing.append(symbol)
            continue
        rows.extend(_to_rows(symbol, sub))

    if still_missing:
        log.warning("Keine Kursdaten erhalten fuer: %s", ", ".join(still_missing))
    return rows, still_missing


def _download(symbols: list[str], start: dt.date, end_exclusive: dt.date) -> pd.DataFrame | None:
    try:
        return yf.download(
            tickers=symbols,
            start=start.isoformat(),
            end=end_exclusive.isoformat(),
            interval="1d",
            auto_adjust=True,
            actions=False,
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception as exc:  # yfinance wirft je nach Version Unterschiedliches
        log.warning("yfinance-Download fehlgeschlagen (%s): %s", ", ".join(symbols), exc)
        return None


def _extract(frame: pd.DataFrame | None, symbol: str, *, single: bool) -> pd.DataFrame | None:
    if frame is None or frame.empty:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        if symbol not in frame.columns.get_level_values(0):
            return None
        return frame[symbol].dropna(how="all")
    if not single:
        return None
    return frame.dropna(how="all")


def _to_rows(symbol: str, frame: pd.DataFrame) -> list[PriceRow]:
    rows: list[PriceRow] = []
    for index, record in frame.iterrows():
        day = _to_date(index)
        close = _as_float(record.get("Close"))
        if day is None or close is None:
            continue
        volume = _as_float(record.get("Volume"))
        rows.append(
            PriceRow(
                symbol=symbol,
                date=day,
                open=_as_float(record.get("Open")),
                high=_as_float(record.get("High")),
                low=_as_float(record.get("Low")),
                close=close,
                volume=int(volume) if volume is not None else None,
            )
        )
    return rows


def _to_date(value: object) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return pd.Timestamp(value).date()  # type: ignore[arg-type]
    except Exception:
        return None


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return num if num == num else None
