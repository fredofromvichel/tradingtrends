"""Kommandozeile fuer Wartung und Diagnose.

    docker compose exec -u app app python -m app.cli run    # Pipeline-Lauf
    docker compose exec -u app app python -m app.cli check  # Konfiguration + Quellen
    docker compose exec -u app app python -m app.cli status # Bestand zusammenfassen
    docker compose exec -u app app python -m app.cli seed --days 150   # Saison nachholen
"""

from __future__ import annotations

import argparse
import dataclasses
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


def _print_summary(result) -> None:
    print(
        f"Status: {result.status} | neue Events: {result.events_ingested} "
        f"| Kurszeilen: {result.prices_ingested} neu von {result.prices_fetched} "
        f"abgerufen | eroeffnet: {result.signals_opened} "
        f"| geschlossen: {result.signals_closed}"
    )


def cmd_run(_args) -> int:
    settings = _prepare()
    result = run_daily(settings, trigger="cli")
    _print_summary(result)
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


def cmd_seed(args) -> int:
    """Einmalig weit zurueckliegende Earnings nachladen.

    Im Normalbetrieb schaut die Pipeline nur ``lookback_days_earnings`` Tage
    zurueck. Ausserhalb der Berichtssaison findet sie dabei korrekterweise
    nichts. Dieser Befehl zieht das Fenster einmalig auf und holt damit die
    letzte Saison nach, damit sich die Kette End-to-End pruefen laesst, statt
    auf die naechste zu warten.
    """
    if args.days < 1:
        print("--days muss mindestens 1 sein.", file=sys.stderr)
        return 2

    settings = load_settings()
    configure_logging(settings.log_level)

    # Die Kurshistorie muss das Ereignisfenster PLUS die Haltedauer abdecken,
    # sonst fehlt der Ausstiegskurs. 60 Kalendertage Puffer fuer ~20 Handelstage
    # plus Wochenenden und Feiertage.
    backfill = max(settings.price_backfill_days, args.days + 60)
    seeded = dataclasses.replace(
        settings,
        lookback_days_earnings=args.days,
        price_backfill_days=backfill,
    )

    print(f"Seed-Lauf: Earnings der letzten {args.days} Tage, "
          f"Kurshistorie {backfill} Tage, {len(seeded.tickers)} Ticker.")
    print("Das kann ein bis zwei Minuten dauern (zwei Finnhub-Requests je Ticker).")

    init_engine(seeded.db_path)
    with session_scope() as session:
        sync_tickers(session, seeded.tickers)

    result = run_daily(seeded, trigger="seed", force_price_backfill=True)
    _print_summary(result)

    if result.errors:
        # Der Earnings-Abruf ist der Kern dieses Befehls. Schlaegt er fehl,
        # sagt das Ergebnis nichts ueber die Datenlage aus.
        print("")
        print("Der Seed-Lauf ist NICHT vollstaendig durchgelaufen:")
        for err in result.errors:
            print(f"  ! {err}")
        print("")
        print("Erst 'python -m app.cli check' gruen bekommen, dann erneut seeden.")
        return 1

    if result.events_ingested == 0:
        print("")
        print("Keine Earnings gefunden. Moegliche Ursachen:")
        print(f"  - Das Fenster von {args.days} Tagen ist zu kurz. "
              "Mit groesserem --days erneut versuchen.")
        print("  - Der Endpunkt /calendar/earnings ist im Tarif gesperrt "
              "('python -m app.cli check' zeigt es).")
        print("  - Die Ticker sind keine US-Titel; Finnhubs Free-Tier deckt "
              "andere Maerkte kaum ab.")
        return 0

    print("")
    print(f"Fertig. {result.signals_opened} Signal(e) angelegt, davon "
          f"{result.signals_closed} bereits geschlossen (Haltedauer abgelaufen).")
    print("Zu sehen unter / (offen), /history (geschlossen) und /events (Rohdaten).")
    print("")
    print("WICHTIG: Das ist KEIN sauberer Backtest. Gerechnet wird mit den Zahlen,")
    print("wie sie HEUTE bei Finnhub stehen - Konsensschaetzungen und berichtete EPS")
    print("werden nachtraeglich revidiert, Kurse sind auf den heutigen Stand")
    print("bereinigt. Das belegt, dass die Pipeline rechnet, nicht dass die")
    print("Strategie traegt. Belastbar ist nur das Forward-Tracking ab jetzt.")
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
    seed = sub.add_parser(
        "seed",
        help="Einmalig die letzte Berichtssaison nachladen (grosses Rueckblickfenster)",
    )
    seed.add_argument(
        "--days",
        type=int,
        default=150,
        help="Wie viele Kalendertage zurueck Earnings gesucht werden (Default: 150)",
    )
    seed.set_defaults(func=cmd_seed)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
