"""Logging auf stdout, ein Format fuer App und uvicorn."""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for noisy in ("apscheduler.executors.default", "httpx", "httpcore", "yfinance",
                  "urllib3", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
