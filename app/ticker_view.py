"""Zusammenstellung der Titel-Detailseite.

Buendelt, was das System ueber einen Titel weiss: den PEAD-Status (das
Einzige, worueber diese Anwendung ein Urteil faellt), den Kurskontext, die
Signalhistorie, den naechsten Meldetermin, das Analystenbild und die
Recherche-Links.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.charting import PriceChart, build_price_chart
from app.config import Settings
from app.indicators import PriceContext, PricePoint, compute_context
from app.models import AnalystRecommendation, EarningsEvent, Price, Signal, Ticker
from app.research import ResearchLink, build_research_links
from app.signals import OPEN, trading_days_elapsed
from app.views import SignalView, _build

# Wie viele Handelstage das Diagramm zeichnet (rund ein Jahr).
CHART_BARS = 252
# Wie viele die Kennzahlen sehen. Muss groesser sein: die 12-Monats-Rendite
# braucht 252 Abstaende, also 253 Kurse.
CONTEXT_BARS = 400


@dataclass
class PeadStatus:
    """Was die PEAD-Logik heute ueber diesen Titel sagt - oft: nichts."""

    state: str          # "ACTIVE" | "RECENT_NO_SIGNAL" | "WINDOW_CLOSED" | "NO_DATA"
    headline: str
    detail: str
    signal: SignalView | None = None
    last_event: dict | None = None
    days_since_report: int | None = None


@dataclass
class AnalystView:
    period: str
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int
    total: int
    fetched_at: dt.datetime | None

    @property
    def buy_share(self) -> float | None:
        return (self.strong_buy + self.buy) / self.total if self.total else None


@dataclass
class TickerDetail:
    symbol: str
    company: str | None
    industry: str | None
    exchange: str | None
    active: bool
    context: PriceContext
    chart: PriceChart
    pead: PeadStatus
    open_signals: list[SignalView]
    closed_signals: list[SignalView]
    events: list[dict]
    next_earnings_date: dt.date | None
    days_to_earnings: int | None
    analysts: AnalystView | None
    analyst_history: list[AnalystView]
    research: list[ResearchLink]


def _price_points(session: Session, symbol: str, limit: int = CONTEXT_BARS) -> list[PricePoint]:
    rows = session.scalars(
        select(Price).where(Price.symbol == symbol).order_by(Price.date.desc()).limit(limit)
    ).all()
    return [PricePoint(r.date, r.close, r.volume) for r in reversed(rows)]


def _pead_status(
    session: Session,
    settings: Settings,
    symbol: str,
    open_signals: list[SignalView],
    events: list[dict],
) -> PeadStatus:
    """Formuliert den Status so, dass 'keine Aussage' auch als solche dasteht."""
    if open_signals:
        signal = open_signals[0]
        remaining = signal.days_remaining
        return PeadStatus(
            state="ACTIVE",
            headline=f"{signal.signal_type}-Signal aktiv",
            detail=(
                f"Ausgelöst durch die Meldung vom {signal.trigger_date}. "
                + (
                    f"Noch {remaining} von {signal.holding_period_days} Handelstagen."
                    if remaining is not None
                    else "Einstiegskurs steht noch aus."
                )
            ),
            signal=signal,
        )

    if not events:
        return PeadStatus(
            state="NO_DATA",
            headline="Keine Aussage möglich",
            detail=(
                "Für diesen Titel wurde noch keine Quartalsmeldung erfasst. "
                "Die PEAD-Logik braucht eine Gewinnüberraschung als Auslöser."
            ),
        )

    latest = events[0]
    report_date = dt.date.fromisoformat(latest["report_date"])
    days_since = (dt.date.today() - report_date).days
    triggered = session.scalar(
        select(Signal).where(Signal.earnings_event_id == latest["id"])
    )

    if triggered is not None:
        return PeadStatus(
            state="WINDOW_CLOSED",
            headline="Keine Aussage möglich",
            detail=(
                f"Das Drift-Fenster der Meldung vom {report_date} ist abgelaufen; "
                "die Position wurde geschlossen. Bis zur nächsten Meldung hat die "
                "PEAD-Logik zu diesem Titel nichts zu sagen."
            ),
            last_event=latest,
            days_since_report=days_since,
        )

    sue = latest.get("sue")
    surprise = latest.get("surprise_pct")
    if sue is not None:
        reason = (
            f"SUE lag bei {sue:+.2f} und damit zwischen den Schwellen "
            f"{settings.sue_threshold_sell} und {settings.sue_threshold_buy}."
        )
    elif surprise is not None:
        reason = (
            f"Die Überraschung lag bei {surprise * 100:+.1f} % und damit innerhalb "
            f"der Fallback-Schwellen von ±{settings.surprise_pct_fallback_buy * 100:.0f} %."
        )
    else:
        reason = "Zur Meldung lagen keine auswertbaren Gewinnzahlen vor."

    return PeadStatus(
        state="RECENT_NO_SIGNAL",
        headline="Kein Signal",
        detail=(
            f"Letzte Meldung am {report_date} (vor {days_since} Tagen). {reason} "
            "Eine Überraschung innerhalb der Schwellen ist der Normalfall."
        ),
        last_event=latest,
        days_since_report=days_since,
    )


def _analyst_views(session: Session, symbol: str) -> list[AnalystView]:
    rows = session.scalars(
        select(AnalystRecommendation)
        .where(AnalystRecommendation.symbol == symbol)
        .order_by(AnalystRecommendation.period.desc())
        .limit(6)
    ).all()
    out: list[AnalystView] = []
    for r in rows:
        total = r.strong_buy + r.buy + r.hold + r.sell + r.strong_sell
        out.append(
            AnalystView(
                period=r.period.isoformat(),
                strong_buy=r.strong_buy,
                buy=r.buy,
                hold=r.hold,
                sell=r.sell,
                strong_sell=r.strong_sell,
                total=total,
                fetched_at=r.fetched_at,
            )
        )
    return out


def ticker_detail(session: Session, settings: Settings, symbol: str) -> TickerDetail | None:
    ticker = session.get(Ticker, symbol.upper())
    if ticker is None:
        return None

    signals = session.scalars(
        select(Signal).where(Signal.symbol == ticker.symbol).order_by(Signal.trigger_date.desc())
    ).all()
    open_signals = [_build(session, s) for s in signals if s.status == OPEN]
    closed_signals = [_build(session, s) for s in signals if s.status != OPEN]

    event_rows = session.scalars(
        select(EarningsEvent)
        .where(EarningsEvent.symbol == ticker.symbol)
        .order_by(EarningsEvent.report_date.desc())
        .limit(12)
    ).all()
    events = [
        {
            "id": e.id,
            "report_date": e.report_date.isoformat(),
            "report_hour": e.report_hour,
            "period": e.period,
            "eps_estimate": e.eps_estimate,
            "eps_actual": e.eps_actual,
            "surprise_pct": e.surprise_pct,
            "sue": e.sue,
        }
        for e in event_rows
    ]

    points = _price_points(session, ticker.symbol)
    chart_points = points[-CHART_BARS:]

    # Ereignismarker: Signale tragen ihre Richtung, Meldungen ohne Signal
    # bleiben neutral - so ist im Chart sichtbar, wo die Logik zugegriffen hat.
    signal_by_event = {s.earnings_event_id: s for s in signals}
    markers: list[tuple[dt.date, str, str]] = []
    for event in event_rows:
        signal = signal_by_event.get(event.id)
        if signal is not None:
            markers.append(
                (
                    signal.trigger_date,
                    signal.signal_type,
                    f"{signal.signal_type}-Signal · Meldung {event.report_date}",
                )
            )
        else:
            markers.append(
                (event.report_date, "NEUTRAL", f"Meldung ohne Signal · {event.report_date}")
            )

    analyst_history = _analyst_views(session, ticker.symbol)
    days_to_earnings = (
        (ticker.next_earnings_date - dt.date.today()).days
        if ticker.next_earnings_date
        else None
    )

    return TickerDetail(
        symbol=ticker.symbol,
        company=ticker.name,
        industry=ticker.industry,
        exchange=ticker.exchange,
        active=ticker.active,
        context=compute_context(points),
        chart=build_price_chart(chart_points, markers),
        pead=_pead_status(session, settings, ticker.symbol, open_signals, events),
        open_signals=open_signals,
        closed_signals=closed_signals,
        events=events,
        next_earnings_date=ticker.next_earnings_date,
        days_to_earnings=days_to_earnings,
        analysts=analyst_history[0] if analyst_history else None,
        analyst_history=analyst_history,
        research=build_research_links(
            ticker.symbol,
            ticker.name,
            ticker.industry,
            company_event_terms=list(settings.research.company_event_terms),
            sector_event_terms=list(settings.research.sector_event_terms),
            analyst_terms=list(settings.research.analyst_terms),
            industry_mapping=settings.research.industry_terms,
            locale=settings.research.locale,
            recent_days=settings.research.recent_days,
            context_days=settings.research.context_days,
        ),
    )


def ticker_overview(session: Session, settings: Settings) -> list[dict]:
    """Kurzstatus aller aktiven Titel fuer die Navigation."""
    rows = session.scalars(
        select(Ticker).where(Ticker.active.is_(True)).order_by(Ticker.symbol)
    ).all()
    out: list[dict] = []
    today = dt.date.today()
    for ticker in rows:
        open_signal = session.scalar(
            select(Signal).where(Signal.symbol == ticker.symbol, Signal.status == OPEN)
        )
        latest_price = session.scalar(
            select(Price)
            .where(Price.symbol == ticker.symbol)
            .order_by(Price.date.desc())
            .limit(1)
        )
        remaining = None
        if open_signal is not None and open_signal.entry_date is not None:
            dates = list(
                session.scalars(
                    select(Price.date)
                    .where(Price.symbol == ticker.symbol)
                    .order_by(Price.date)
                ).all()
            )
            remaining = max(
                0,
                open_signal.holding_period_days
                - trading_days_elapsed(dates, open_signal.entry_date),
            )
        out.append(
            {
                "symbol": ticker.symbol,
                "company": ticker.name,
                "industry": ticker.industry,
                "signal_type": open_signal.signal_type if open_signal else None,
                "days_remaining": remaining,
                "last_close": latest_price.close if latest_price else None,
                "next_earnings_date": (
                    ticker.next_earnings_date.isoformat()
                    if ticker.next_earnings_date
                    else None
                ),
                "days_to_earnings": (
                    (ticker.next_earnings_date - today).days
                    if ticker.next_earnings_date
                    else None
                ),
            }
        )
    return out
