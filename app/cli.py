"""Kommandozeile fuer Wartung und Diagnose.

    docker compose exec app python -m app.cli run      # Pipeline-Lauf
    docker compose exec app python -m app.cli check    # Konfiguration + Finnhub pruefen
    docker compose exec app python -m app.cli status   # Bestand kurz zusammenfassen
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from app.config import ConfigError, load_settings
from app.db import init_engine, session_scope, sync_tickers
from app.logging_conf import configure_logging
from app.pipeline import run_daily
from app.sources.finnhub_client import (
    FinnhubClient,
    FinnhubError,
    FinnhubTransientError,
)
from app.views import summary


def _prepare():
    settings = load_settings()
    configure_logging(settings.log_level)
    init_engine(settings.db_path)
    with session_scope() as session:
        sync_tickers(session, settings.tickers)
    return settings


def cmd_run(_args) -> int:
    settings = _prepare()
    result = run_daily(settings, trigger="cli")
    print(
        f"Status: {result.status} | neue Events: {result.events_ingested} "
        f"| Kurszeilen: {result.prices_ingested} | eroeffnet: {result.signals_opened} "
        f"| geschlossen: {result.signals_closed}"
    )
    for err in result.errors:
        print(f"  ! {err}", file=sys.stderr)
    return 0 if result.status in {"OK", "PARTIAL"} else 1


def cmd_check(_args) -> int:
    settings = load_settings()
    configure_logging(settings.log_level)
    print(f"Konfiguration OK. {len(settings.tickers)} Ticker: {', '.join(settings.tickers)}")
    print(f"Datenbank: {settings.db_path}")
    print(f"Scheduler: {'an' if settings.schedule.enabled else 'aus'} "
          f"({settings.schedule.cron}, {settings.schedule.timezone})")

    probe = settings.tickers[0]
    today = dt.date.today()
    with FinnhubClient(
        settings.finnhub_api_key,
        min_interval_seconds=settings.finnhub_min_interval_seconds,
    ) as client:
        try:
            history = client.earnings_surprises(probe)
            print(f"Finnhub /stock/earnings  : OK ({len(history)} Quartale fuer {probe})")
        except (FinnhubError, FinnhubTransientError) as exc:
            print(f"Finnhub /stock/earnings  : FEHLER - {exc}", file=sys.stderr)
            return 1
        try:
            cal = client.earnings_calendar(probe, today - dt.timedelta(days=120), today)
            print(f"Finnhub /calendar/earnings: OK ({len(cal)} Eintraege fuer {probe})")
        except (FinnhubError, FinnhubTransientError) as exc:
            print(f"Finnhub /calendar/earnings: nicht verfuegbar - {exc}")
            print("  -> Die Pipeline weicht dann auf die Fiskalperiode aus (Naeherung).")

    try:
        from app.sources.prices import fetch_prices

        rows, missing = fetch_prices([probe], today - dt.timedelta(days=10), today)
        if rows:
            print(f"yfinance                 : OK ({len(rows)} Kurstage fuer {probe}, "
                  f"letzter {rows[-1].date} = {rows[-1].close:.2f})")
        else:
            print(f"yfinance                 : FEHLER - keine Daten fuer {missing}",
                  file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"yfinance                 : FEHLER - {exc}", file=sys.stderr)
        return 1

    return 0


def cmd_status(_args) -> int:
    _prepare()
    with session_scope() as session:
        data = summary(session)
    for key, value in data.items():
        print(f"{key:>18}: {value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="PEAD-Signal-POC")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Einen Pipeline-Lauf ausfuehren").set_defaults(func=cmd_run)
    sub.add_parser("check", help="Konfiguration und Datenquellen pruefen").set_defaults(
        func=cmd_check
    )
    sub.add_parser("status", help="Bestand zusammenfassen").set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
