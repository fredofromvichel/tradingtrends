"""Konfiguration aus config.yaml plus Umgebungsvariablen."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config.yaml"))


class ConfigError(RuntimeError):
    """Konfiguration ist unbrauchbar - der Prozess soll sofort abbrechen."""


@dataclass(frozen=True)
class Schedule:
    enabled: bool = True
    cron: str = "30 22 * * 1-5"
    timezone: str = "Europe/Berlin"


@dataclass(frozen=True)
class Momentum:
    """Parameter der zweiten Signalquelle. Getrennt von den PEAD-Schwellen."""

    enabled: bool = True
    lookback_days: int = 252     # Formationsfenster in Handelstagen (~12 Monate)
    skip_days: int = 21          # juengster Monat wird ausgelassen
    group_fraction: float = 0.3  # Anteil je Gruppe (0.3 == Terzile)
    holding_period_days: int = 21
    min_universe: int = 8
    rebalance: str = "monthly"   # aktuell nur "monthly"


@dataclass(frozen=True)
class Research:
    """Begriffe und Zeitfenster der Recherche-Links."""

    locale: dict[str, str] = field(
        default_factory=lambda: {"hl": "de", "gl": "DE", "ceid": "DE:de"}
    )
    recent_days: int = 7
    context_days: int = 30
    company_event_terms: tuple[str, ...] = ()
    sector_event_terms: tuple[str, ...] = ()
    analyst_terms: tuple[str, ...] = ()
    industry_terms: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    # Startliste fuer eine leere Datenbank. Beobachtet wird, was in der
    # Tabelle tickers aktiv ist - siehe app/watchlist.py.
    tickers: tuple[str, ...]
    finnhub_api_key: str
    db_path: Path
    sue_threshold_buy: float = 1.0
    sue_threshold_sell: float = -1.0
    surprise_pct_fallback_buy: float = 0.05
    surprise_pct_fallback_sell: float = -0.05
    min_history_for_sue: int = 4
    holding_period_days: int = 20
    lookback_days_earnings: int = 3
    price_backfill_days: int = 180
    price_refresh_days: int = 7
    finnhub_min_interval_seconds: float = 0.25
    run_on_startup: bool = False
    log_level: str = "INFO"
    outlook_max_age_days: int = 3
    finnhub_recheck_days: int = 14
    # Yahoo als Ausweichquelle, wo Finnhubs Tarif nicht greift.
    earnings_fallback_enabled: bool = True
    # Absender-IPs, die zugreifen duerfen. Kommt aus der Umgebung, nicht aus
    # config.yaml - es ist eine Betriebseinstellung, keine Fachlogik.
    allowed_ips: str = ""
    # Wohin der Port auf dem Host veroeffentlicht wurde. Nur wenn das nicht
    # Loopback ist, muss die Allowlist greifen.
    bind_addr: str = "127.0.0.1"
    schedule: Schedule = field(default_factory=Schedule)
    research: Research = field(default_factory=Research)
    momentum: Momentum = field(default_factory=Momentum)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(path: Path | None = None) -> Settings:
    """Liest config.yaml, ueberlagert mit Env-Variablen und validiert."""
    path = path or CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} enthaelt kein YAML-Mapping.")

    tickers = [str(t).strip().upper() for t in raw.get("tickers") or [] if str(t).strip()]
    # Reihenfolge erhalten, Duplikate entfernen.
    tickers = list(dict.fromkeys(tickers))
    # Leer ist erlaubt: die Liste ist nur die Startliste fuer eine leere
    # Datenbank, gepflegt wird sie in der Oberflaeche (app/watchlist.py).

    api_key = (os.getenv("FINNHUB_API_KEY") or "").strip()
    if not api_key:
        raise ConfigError(
            "FINNHUB_API_KEY ist nicht gesetzt. Key in der .env eintragen "
            "(Vorlage: .env.example) und Container neu starten."
        )

    sched_raw = raw.get("schedule") or {}
    if not isinstance(sched_raw, dict):
        raise ConfigError("Der Abschnitt 'schedule' in config.yaml ist kein Mapping.")
    schedule = Schedule(
        enabled=bool(sched_raw.get("enabled", True)),
        cron=str(sched_raw.get("cron", "30 22 * * 1-5")),
        timezone=str(sched_raw.get("timezone", "Europe/Berlin")),
    )

    research_raw = raw.get("research") or {}
    if not isinstance(research_raw, dict):
        raise ConfigError("Der Abschnitt 'research' in config.yaml ist kein Mapping.")
    locale_raw = research_raw.get("locale") or {}
    research = Research(
        locale={
            "hl": str(locale_raw.get("hl", "de")),
            "gl": str(locale_raw.get("gl", "DE")),
            "ceid": str(locale_raw.get("ceid", "DE:de")),
        },
        recent_days=int(research_raw.get("recent_days", 7)),
        context_days=int(research_raw.get("context_days", 30)),
        company_event_terms=tuple(research_raw.get("company_event_terms") or ()),
        sector_event_terms=tuple(research_raw.get("sector_event_terms") or ()),
        analyst_terms=tuple(research_raw.get("analyst_terms") or ()),
        industry_terms={
            str(k): [str(t) for t in (v or [])]
            for k, v in (research_raw.get("industry_terms") or {}).items()
        },
    )

    momentum_raw = raw.get("momentum") or {}
    if not isinstance(momentum_raw, dict):
        raise ConfigError("Der Abschnitt 'momentum' in config.yaml ist kein Mapping.")
    momentum = Momentum(
        enabled=bool(momentum_raw.get("enabled", True)),
        lookback_days=int(momentum_raw.get("lookback_days", 252)),
        skip_days=int(momentum_raw.get("skip_days", 21)),
        group_fraction=float(momentum_raw.get("group_fraction", 0.3)),
        holding_period_days=int(momentum_raw.get("holding_period_days", 21)),
        min_universe=int(momentum_raw.get("min_universe", 8)),
        rebalance=str(momentum_raw.get("rebalance", "monthly")),
    )

    settings = Settings(
        tickers=tuple(tickers),
        finnhub_api_key=api_key,
        db_path=Path(os.getenv("DB_PATH", "/data/poc.db")),
        sue_threshold_buy=float(raw.get("sue_threshold_buy", 1.0)),
        sue_threshold_sell=float(raw.get("sue_threshold_sell", -1.0)),
        surprise_pct_fallback_buy=float(raw.get("surprise_pct_fallback_buy", 0.05)),
        surprise_pct_fallback_sell=float(raw.get("surprise_pct_fallback_sell", -0.05)),
        min_history_for_sue=int(raw.get("min_history_for_sue", 4)),
        holding_period_days=int(raw.get("holding_period_days", 20)),
        lookback_days_earnings=int(raw.get("lookback_days_earnings", 3)),
        price_backfill_days=int(raw.get("price_backfill_days", 180)),
        price_refresh_days=int(raw.get("price_refresh_days", 7)),
        finnhub_min_interval_seconds=float(raw.get("finnhub_min_interval_seconds", 0.25)),
        run_on_startup=_as_bool(os.getenv("RUN_ON_STARTUP"), False),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
        outlook_max_age_days=int(raw.get("outlook_max_age_days", 3)),
        finnhub_recheck_days=int(raw.get("finnhub_recheck_days", 14)),
        earnings_fallback_enabled=_as_bool(
            str(raw.get("earnings_fallback_enabled", True)), True
        ),
        allowed_ips=(os.getenv("ALLOWED_IPS") or "").strip(),
        bind_addr=(os.getenv("BIND_ADDR") or "127.0.0.1").strip(),
        schedule=schedule,
        research=research,
        momentum=momentum,
    )
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    if s.sue_threshold_buy <= s.sue_threshold_sell:
        raise ConfigError("sue_threshold_buy muss groesser als sue_threshold_sell sein.")
    if s.surprise_pct_fallback_buy <= s.surprise_pct_fallback_sell:
        raise ConfigError(
            "surprise_pct_fallback_buy muss groesser als surprise_pct_fallback_sell sein."
        )
    if abs(s.surprise_pct_fallback_buy) > 1 or abs(s.surprise_pct_fallback_sell) > 1:
        raise ConfigError(
            "surprise_pct_fallback_* werden als Bruch erwartet (0.05 == 5 %), "
            "nicht als Prozentzahl."
        )
    if s.holding_period_days < 1:
        raise ConfigError("holding_period_days muss mindestens 1 sein.")
    if s.min_history_for_sue < 2:
        raise ConfigError("min_history_for_sue muss mindestens 2 sein (Standardabweichung).")
    if s.research.recent_days < 1 or s.research.context_days < 1:
        raise ConfigError("research.recent_days und research.context_days muessen >= 1 sein.")
    if s.outlook_max_age_days < 1:
        raise ConfigError("outlook_max_age_days muss mindestens 1 sein.")
    if s.finnhub_recheck_days < 1:
        raise ConfigError("finnhub_recheck_days muss mindestens 1 sein.")
    if s.price_backfill_days < s.holding_period_days * 2:
        raise ConfigError(
            "price_backfill_days ist zu klein: die Kurshistorie muss die Haltedauer "
            "deutlich ueberdecken."
        )

    m = s.momentum
    if m.enabled:
        if m.skip_days >= m.lookback_days:
            raise ConfigError(
                "momentum.skip_days muss kleiner als momentum.lookback_days sein."
            )
        if m.lookback_days < 2:
            raise ConfigError("momentum.lookback_days muss mindestens 2 sein.")
        if not 0 < m.group_fraction < 0.5:
            raise ConfigError(
                "momentum.group_fraction muss zwischen 0 und 0.5 liegen - bei 0.5 "
                "waeren Spitze und Schluss dasselbe."
            )
        if m.holding_period_days < 1:
            raise ConfigError("momentum.holding_period_days muss mindestens 1 sein.")
        if m.min_universe < 4:
            raise ConfigError(
                "momentum.min_universe muss mindestens 4 sein - darunter besteht "
                "jede Gruppe aus einem einzigen Titel."
            )
        # Formationsfenster in Kalendertage umrechnen: rund 1.45 Kalendertage
        # je Handelstag, plus Puffer.
        needed = int((m.lookback_days + m.holding_period_days) * 1.45) + 30
        if s.price_backfill_days < needed:
            raise ConfigError(
                f"price_backfill_days ({s.price_backfill_days}) reicht fuer das "
                f"Momentum-Formationsfenster nicht aus - noetig sind etwa {needed} "
                "Kalendertage. Wert erhoehen und 'make backfill' ausfuehren, oder "
                "momentum.enabled auf false setzen."
            )
