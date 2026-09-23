"""Finnhub-Anbindung: Earnings-Kalender und Surprise-Historie.

Zwei Endpunkte werden genutzt:

* ``/calendar/earnings``  liefert das **Meldedatum** (report_date) - das ist der
  Zeitpunkt, an dem ein PEAD-Signal ausgeloest wird.
* ``/stock/earnings``     liefert die letzten Quartale mit actual/estimate und
  dient als Historie fuer die SUE-Standardabweichung. Faellt ausserdem als
  Ersatzquelle ein, wenn der Kalender im Free-Tier gesperrt ist.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Die Satzform ist quellenneutral und liegt deshalb nicht hier. Der Re-Export
# haelt bestehende Importe aus diesem Modul gueltig.
from app.sources.earnings import FINNHUB, RawEarnings  # noqa: F401

log = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"


class FinnhubError(RuntimeError):
    """Nicht wiederholbarer Fehler (Key ungueltig, Endpunkt gesperrt, ...)."""


class FinnhubAuthError(FinnhubError):
    """Key fehlt oder ist ungueltig - betrifft alle Symbole, nicht nur eines."""


class FinnhubForbiddenError(FinnhubError):
    """Endpunkt ist fuer diesen Tarif nicht freigeschaltet (403).

    Strukturell, nicht voruebergehend: ein Retry aendert nichts, und beim
    naechsten Lauf ebenso wenig. Traegt den Endpunkt mit, damit die Pipeline
    sich die Sperre je Symbol merken kann.
    """

    def __init__(self, message: str, endpoint: str) -> None:
        super().__init__(message)
        self.endpoint = endpoint


class FinnhubTransientError(RuntimeError):
    """Vorruebergehender Fehler - Retry sinnvoll (429, 5xx, Timeout)."""


@dataclass(frozen=True)
class CompanyProfile:
    symbol: str
    name: str | None
    industry: str | None
    exchange: str | None


@dataclass(frozen=True)
class Recommendation:
    """Analystenverteilung eines Monats."""

    symbol: str
    period: dt.date
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int

    @property
    def total(self) -> int:
        return self.strong_buy + self.buy + self.hold + self.sell + self.strong_sell


class FinnhubClient:
    def __init__(
        self,
        api_key: str,
        *,
        min_interval_seconds: float = 0.25,
        timeout: float = 20.0,
    ) -> None:
        if not api_key:
            raise FinnhubAuthError("Kein Finnhub-API-Key gesetzt.")
        self._api_key = api_key
        self._min_interval = max(0.0, min_interval_seconds)
        self._client = httpx.Client(timeout=timeout, headers={"X-Finnhub-Token": api_key})
        self._lock = threading.Lock()
        self._last_call = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FinnhubClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- HTTP ---------------------------------------------------------------

    def _throttle(self) -> None:
        """Haelt den Free-Tier (60 Calls/Min) ein."""
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    @retry(
        retry=retry_if_exception_type(FinnhubTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, str]) -> dict | list:
        self._throttle()
        url = f"{BASE_URL}{path}"
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise FinnhubTransientError(f"Netzwerkfehler bei {path}: {exc}") from exc

        if resp.status_code == 401:
            raise FinnhubAuthError(
                "Finnhub lehnt den API-Key ab (401). FINNHUB_API_KEY in der .env pruefen."
            )
        if resp.status_code == 403:
            raise FinnhubForbiddenError(
                f"Finnhub verweigert den Zugriff auf {path} (403) - der Endpunkt ist "
                "fuer diesen Tarif nicht freigeschaltet.",
                endpoint=path,
            )
        if resp.status_code == 429:
            raise FinnhubTransientError("Finnhub-Rate-Limit erreicht (429).")
        if resp.status_code >= 500:
            raise FinnhubTransientError(f"Finnhub-Serverfehler {resp.status_code} bei {path}.")
        if resp.status_code != 200:
            raise FinnhubError(f"Unerwarteter Status {resp.status_code} bei {path}.")

        try:
            return resp.json()
        except ValueError as exc:
            raise FinnhubTransientError(f"Antwort von {path} ist kein JSON.") from exc

    # -- Endpunkte ----------------------------------------------------------

    def earnings_calendar(
        self, symbol: str, date_from: dt.date, date_to: dt.date
    ) -> list[RawEarnings]:
        """Gemeldete Earnings im Zeitfenster [date_from, date_to]."""
        payload = self._get(
            "/calendar/earnings",
            {
                "symbol": symbol,
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
            },
        )
        items = (payload or {}).get("earningsCalendar") or [] if isinstance(payload, dict) else []
        out: list[RawEarnings] = []
        for item in items:
            report_date = _parse_date(item.get("date"))
            if report_date is None:
                continue
            out.append(
                RawEarnings(
                    symbol=symbol,
                    report_date=report_date,
                    eps_estimate=_as_float(item.get("epsEstimate")),
                    eps_actual=_as_float(item.get("epsActual")),
                    surprise_percent=_as_float(item.get("surprisePercent")),
                    period=str(item.get("quarter") or "") or None,
                    hour=(str(item.get("hour") or "").strip().lower() or None),
                )
            )
        return out

    def company_profile(self, symbol: str) -> CompanyProfile:
        """Stammdaten fuer die Anzeige - ausgeschriebener Name, Branche, Boerse."""
        payload = self._get("/stock/profile2", {"symbol": symbol})
        data = payload if isinstance(payload, dict) else {}
        return CompanyProfile(
            symbol=symbol,
            name=_as_text(data.get("name"), 128),
            industry=_as_text(data.get("finnhubIndustry"), 96),
            exchange=_as_text(data.get("exchange"), 96),
        )

    def recommendations(self, symbol: str) -> list[Recommendation]:
        """Analystenbild der letzten Monate, neueste zuerst."""
        payload = self._get("/stock/recommendation", {"symbol": symbol})
        items = payload if isinstance(payload, list) else []
        out: list[Recommendation] = []
        for item in items:
            period = _parse_date(item.get("period"))
            if period is None:
                continue
            out.append(
                Recommendation(
                    symbol=symbol,
                    period=period,
                    strong_buy=_as_int(item.get("strongBuy")),
                    buy=_as_int(item.get("buy")),
                    hold=_as_int(item.get("hold")),
                    sell=_as_int(item.get("sell")),
                    strong_sell=_as_int(item.get("strongSell")),
                )
            )
        out.sort(key=lambda r: r.period, reverse=True)
        return out

    def earnings_surprises(self, symbol: str) -> list[RawEarnings]:
        """Die letzten gemeldeten Quartale (Finnhub liefert ueblich 4-8)."""
        payload = self._get("/stock/earnings", {"symbol": symbol})
        items = payload if isinstance(payload, list) else []
        out: list[RawEarnings] = []
        for item in items:
            period = _parse_date(item.get("period"))
            if period is None:
                continue
            out.append(
                RawEarnings(
                    symbol=symbol,
                    # /stock/earnings kennt nur die Fiskalperiode, nicht das
                    # Meldedatum. Fuer die Historie reicht das; als Ersatzquelle
                    # ist es eine Naeherung (siehe pipeline.py).
                    report_date=period,
                    eps_estimate=_as_float(item.get("estimate")),
                    eps_actual=_as_float(item.get("actual")),
                    surprise_percent=_as_float(item.get("surprisePercent")),
                    period=period.isoformat(),
                    # /stock/earnings kennt den Meldezeitpunkt nicht.
                    hour=None,
                )
            )
        out.sort(key=lambda r: r.report_date, reverse=True)
        return out


def _as_int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_text(value: object, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return num if num == num else None  # NaN aussortieren


def _parse_date(value: object) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
