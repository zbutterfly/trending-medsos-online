"""Structured-ish logging setup. JSON-ish lines for easy parsing."""
from __future__ import annotations

import logging
import sys

from .config import settings


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Reset any handler added by imports.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    fmt = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet down noisy libs.
    for noisy in ("httpx", "httpcore", "apscheduler", "prawcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
