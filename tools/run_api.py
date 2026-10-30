#!/usr/bin/env python
"""Run the Phase 3 development API on the validated loopback bind address.

Step 1.3 deliverable: the settings-driven server entry point for the
API foundation (:mod:`src.api`). Nothing here duplicates or bypasses
loopback validation — the bind address/port are the already-validated
``AppSettings.api_host`` / ``AppSettings.api_port`` values resolved from
the current environment (``API_HOST`` / ``API_PORT``, defaults
``127.0.0.1:8000``; non-loopback values are rejected by the config
layer itself).

Safety contract
---------------
* Loopback-only: a non-loopback ``API_HOST`` makes configuration
  loading fail fast (rejected by :mod:`src.config`), so the server can
  never start bound to a LAN address. This is a development safety
  measure, not a production security boundary; there is no
  authentication and no CORS in the foundation step.
* Single-process, no workers: this runner never spawns uvicorn worker
  processes. A dedicated startup lock guard against multi-process
  serving follows its finalized design contract in a later authorized
  step; until then, running a second instance on a different loopback
  port is permitted and harmless (it is also refused by the OS on port
  conflict with the same port).
* No database activity: starting or importing this runner never
  connects to PostgreSQL (uvicorn is imported lazily inside the run
  path).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

__all__ = ["main", "print_bind"]


class _InvalidStartupConfiguration(Exception):
    """Configuration was rejected by ``src.config`` before settings could
    exist. Classified at the import boundary — the failed configuration
    module is deliberately **not** re-imported, so classification cannot
    retrigger the same validation failure."""


def _load_settings():
    """Load settings, classifying failure at this exact boundary.

    ``src.config`` evaluates its module-level ``SETTINGS`` at import
    time, so an invalid ``API_HOST``/``API_PORT`` raises **during the
    import**. The catch must therefore exist here — before any retry of
    the failed import — rather than after, so classification cannot
    retrigger the same validation failure.

    Only configuration-class failures (``ConfigError``, a ``ValueError``
    subclass — the sole environment-validation failure mode of the
    settings loader) are converted; anything else propagates (never
    swallowed). Sanitization: the runner-facing diagnostic names the
    validated ``API_HOST``/``API_PORT`` fields but never their values or
    any other environment content.
    """
    try:
        from src.config import load_settings

        return load_settings()
    except ValueError:  # ConfigError(self-describing) — validate failures only
        raise _InvalidStartupConfiguration(
            "invalid configuration: API_HOST/API_PORT were rejected "
            "by the validated settings layer (src.config)"
        ) from None


def print_bind(settings) -> None:
    """Print the validated loopback bind address without serving."""
    print(f"bind: {settings.api_host}:{settings.api_port}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Phase 3 development API on the validated loopback "
            "bind address from API_HOST/API_PORT."
        ),
        epilog=(
            "Loopback-only and unauthenticated by design: do not expose "
            "this API to the network or claim production readiness."
        ),
    )
    parser.add_argument(
        "--print-bind",
        action="store_true",
        help="print the validated bind address and exit without serving",
    )
    args = parser.parse_args(argv)

    try:
        settings = _load_settings()
    except _InvalidStartupConfiguration as exc:
        # Sanitized: names the offending fields, never their values or
        # the underlying validation exception (trusted diagnostics stay
        # out of the runner surface).
        print(f"run-api: {exc}", file=sys.stderr)
        return 2

    if args.print_bind:
        print_bind(settings)
        return 0

    # Lazy server-runtime import: importing this module starts nothing.
    from src.api import run_server

    run_server(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
