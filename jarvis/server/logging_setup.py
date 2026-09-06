"""
server/logging_setup.py
-----------------------
Logging for the JARVIS V2 Dell server.

Writes to the console and to a rotating file (<log_dir>/server.log) so a
service-mode install (no visible console) still keeps durable logs.
Idempotent: calling setup_logging() again replaces the handlers cleanly,
which matters for tests and reloads.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

LOG_NAME = "jarvis.server"

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(cfg) -> logging.Logger:
    """Configure console + rotating-file handlers and return the root logger."""
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)

    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(level)
    for handler in list(logger.handlers):  # idempotent re-setup
        logger.removeHandler(handler)
    logger.propagate = False

    fmt = logging.Formatter(_FORMAT)

    stream = sys.stdout if sys.stdout is not None else sys.stderr
    console = logging.StreamHandler(stream)
    console.setLevel(level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        cfg.log_dir / "server.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.info("logging ready -> console + %s", cfg.log_dir / "server.log")
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger of the jarvis.server tree (name is optional)."""
    if name:
        return logging.getLogger(f"{LOG_NAME}.{name}")
    return logging.getLogger(LOG_NAME)
