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
class Settings:
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
    schedule: Schedule = field(default_factory=Schedule)


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
    if not tickers:
        raise ConfigError("In config.yaml ist kein einziger Ticker eingetragen.")

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
        schedule=schedule,
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
    if s.price_backfill_days < s.holding_period_days * 2:
        raise ConfigError(
            "price_backfill_days ist zu klein: die Kurshistorie muss die Haltedauer "
            "deutlich ueberdecken."
        )
