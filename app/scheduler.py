"""In-Process-Scheduler (APScheduler) fuer den taeglichen Lauf."""

from __future__ import annotations

import logging

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.pipeline import PipelineBusy, run_daily

log = logging.getLogger(__name__)

JOB_ID = "pead_daily"


def _job(settings: Settings) -> None:
    try:
        run_daily(settings, trigger="scheduler")
    except PipelineBusy:
        log.warning("Geplanter Lauf uebersprungen - es laeuft bereits einer.")
    except Exception:  # pragma: no cover - der Scheduler darf nie sterben
        log.exception("Geplanter Lauf fehlgeschlagen; der naechste Termin bleibt bestehen.")


def start_scheduler(settings: Settings) -> BackgroundScheduler | None:
    if not settings.schedule.enabled:
        log.info("Scheduler ist per config.yaml deaktiviert.")
        return None

    try:
        timezone = pytz.timezone(settings.schedule.timezone)
    except pytz.UnknownTimeZoneError:
        log.error(
            "Unbekannte Zeitzone '%s' - fallback auf UTC.", settings.schedule.timezone
        )
        timezone = pytz.utc

    try:
        trigger = CronTrigger.from_crontab(settings.schedule.cron, timezone=timezone)
    except ValueError as exc:
        log.error("Ungueltiger Cron-Ausdruck '%s': %s - Scheduler bleibt aus.",
                  settings.schedule.cron, exc)
        return None

    scheduler = BackgroundScheduler(timezone=timezone, job_defaults={
        # Nach einem Ausfall nicht mehrere verpasste Laeufe nachholen.
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 3600,
    })
    scheduler.add_job(_job, trigger=trigger, args=[settings], id=JOB_ID, name="PEAD Tageslauf")
    scheduler.start()
    job = scheduler.get_job(JOB_ID)
    log.info(
        "Scheduler aktiv: cron='%s' (%s), naechster Lauf %s",
        settings.schedule.cron,
        settings.schedule.timezone,
        job.next_run_time if job else "unbekannt",
    )
    return scheduler
