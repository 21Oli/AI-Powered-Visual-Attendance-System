"""Step 01 environment verification.

These tests verify the foundation established in Phase 1 - Step 01:

* the test run executes inside the project virtual environment,
* every foundation package imports successfully on Python 3.14,
* the central logging setup writes to the configured log directory,
* the project directory skeleton exists.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Virtual environment
# ---------------------------------------------------------------------------


def test_running_inside_virtualenv() -> None:
    """The test interpreter must be the project .venv, not the system Python."""
    assert sys.prefix != sys.base_prefix, (
        "pytest is running with the system interpreter; "
        "use .venv/Scripts/python.exe -m pytest"
    )
    assert Path(sys.prefix).resolve().name == ".venv"


def test_python_version() -> None:
    """The venv must be the Python 3.14 interpreter chosen in Step 01."""
    assert sys.version_info[:2] == (3, 14)


# ---------------------------------------------------------------------------
# Foundation package imports (empirical wheel verification on 3.14)
# ---------------------------------------------------------------------------


def test_numpy_imports() -> None:
    import numpy

    assert numpy.__version__


def test_opencv_imports() -> None:
    import cv2

    assert cv2.__version__


def test_python_dotenv_imports() -> None:
    import dotenv

    assert dotenv.__version__ if hasattr(dotenv, "__version__") else True


# ---------------------------------------------------------------------------
# Logging foundation (src/logging_setup.py)
# ---------------------------------------------------------------------------


def test_get_logger_writes_to_log_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_logger must create the log dir and write a record to app.log."""
    from src.logging_setup import get_logger

    log_dir = tmp_path / "logs"
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    # Unique name per call so a previous test's configured logger is not reused.
    name = f"test.logging.{abs(hash(tmp_path))}"
    logger = get_logger(name)
    assert isinstance(logger, logging.Logger)

    logger.info("step01 verification message")
    for handler in logger.handlers:
        handler.flush()

    log_file = log_dir / "app.log"
    assert log_file.exists(), "rotating file handler did not create app.log"
    assert "step01 verification message" in log_file.read_text(encoding="utf-8")


def test_get_logger_is_idempotent() -> None:
    """Repeated get_logger calls must not duplicate handlers."""
    from src.logging_setup import get_logger

    logger = get_logger("test.idempotency")
    handler_count = len(logger.handlers)
    assert get_logger("test.idempotency") is logger
    assert len(logger.handlers) == handler_count


# ---------------------------------------------------------------------------
# Directory skeleton
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "src",
        "notebooks",
        "data/employees",
        "data/face_samples",
        "data/embeddings",
        "data/logs",
        "models",
        "tests",
        "reports",
        "docs",
    ],
)
def test_directory_skeleton_exists(relative: str) -> None:
    assert (PROJECT_ROOT / relative).is_dir(), f"missing directory: {relative}"
