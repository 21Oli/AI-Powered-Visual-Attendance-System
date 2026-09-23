"""Central logging setup for the AI-Powered Visual Attendance System.

Provides a single entry point, :func:`get_logger`, that configures a
module-level logger with:

* a console handler (stderr),
* a rotating file handler under ``data/logs/`` (5 MB per file, 5 backups).

Design notes
------------
* Callers only use ``logging.getLogger(__name__)``-style names via
  :func:`get_logger`; handler configuration is attached once per logger
  name so repeated calls never duplicate output.
* Configuration comes from environment variables (``.env`` loaded via
  ``python-dotenv``): ``LOG_LEVEL`` (default ``INFO``) and ``LOG_DIR``
  (default ``data/logs``).
* This module is the *only* logging foundation introduced in Step 01.
  Later steps extend configuration (e.g. per-component levels) in
  ``src/config.py`` without changing this contract.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["get_logger", "configure_logging"]

_DEFAULT_LOG_DIR = Path("data") / "logs"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per log file
_BACKUP_COUNT = 5

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _resolve_log_level() -> int:
    """Translate the ``LOG_LEVEL`` environment variable to a logging level."""
    raw = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelName(raw)
    return level if isinstance(level, int) else logging.INFO


def _resolve_log_dir() -> Path:
    """Resolve ``LOG_DIR`` from the environment, defaulting to ``data/logs``."""
    return Path(os.getenv("LOG_DIR", str(_DEFAULT_LOG_DIR)))


def _build_handlers(log_dir: Path) -> list[logging.Handler]:
    """Create the console and rotating-file handlers for a fresh logger."""
    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))

    return [console, file_handler]


def configure_logging(name: str) -> logging.Logger:
    """Configure and return the logger ``name`` exactly once.

    Idempotent: if the logger already has handlers attached, it is returned
    unchanged so repeated :func:`get_logger` calls cannot duplicate records.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(_resolve_log_level())
    logger.propagate = False
    for handler in _build_handlers(_resolve_log_dir()):
        logger.addHandler(handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for ``name`` (typically ``__name__``)."""
    # Load .env if present so LOG_LEVEL / LOG_DIR apply without shell setup.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is a pinned dependency
        pass
    return configure_logging(name)
