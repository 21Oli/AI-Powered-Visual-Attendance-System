#!/usr/bin/env python
"""Operator CLI for S8 — read-only schema and migration integrity verification.

Thin wrapper only: it resolves settings, assembles an
:class:`src.s8.verification.S8Request`, calls the verifier, renders the sanitized
report, and maps the outcome to a stable exit code. It contains no SQL, no
connection handling, and no policy.

Live execution is gated: without ``--authorize-live-verification`` the verifier
refuses with ``BLOCKED — AUTHORIZATION REQUIRED`` **before** parsing any DSN or
attempting any connection. This is a deliberate, separate authorization from
"the code exists"; nothing here can be switched on by an environment variable.

Exit codes (stable)
=================== =======================================================
0                    PASS — VERIFIED
1                    unexpected internal error (no report produced)
2                    BLOCKED — AUTHORIZATION REQUIRED
3                    BLOCKED — TARGET NOT VERIFIED
4                    BLOCKED — DATABASE UNAVAILABLE
5                    FAIL — MIGRATION HISTORY INVALID
6                    FAIL — SCHEMA MISMATCH
7                    BLOCKED — READ-ONLY GUARANTEE NOT ESTABLISHED
=================== =======================================================

The report never contains a DSN, a credential, a hostname, a role name, SQL text,
a driver message, an employee row, or biometric data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

__all__ = ["main"]

EXIT_CODES = {
    "PASS — VERIFIED": 0,
    "BLOCKED — AUTHORIZATION REQUIRED": 2,
    "BLOCKED — TARGET NOT VERIFIED": 3,
    "BLOCKED — DATABASE UNAVAILABLE": 4,
    "FAIL — MIGRATION HISTORY INVALID": 5,
    "FAIL — SCHEMA MISMATCH": 6,
    "BLOCKED — READ-ONLY GUARANTEE NOT ESTABLISHED": 7,
}

_PREFIX = "verify-schema-s8"


def _settings():
    from src.config import load_settings

    return load_settings()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PREFIX,
        description=(
            "Read-only verification of the configured application database "
            "against the pinned migration contract and the physical schema "
            "contract. Takes no advisory lock and never writes."
        ),
    )
    parser.add_argument(
        "--authorize-live-verification",
        action="store_true",
        help=(
            "explicitly authorize one live verification run; without it the "
            "tool refuses before parsing any DSN"
        ),
    )
    parser.add_argument(
        "--target",
        default=None,
        help=(
            "optional restatement of the configured target; it must normalize "
            "to the same database identity as ATTENDANCE_DATABASE_URL and can "
            "never select a different database"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as one JSON object instead of a summary",
    )
    parser.add_argument(
        "--report",
        default=None,
        help=(
            "write the JSON report to this path (refused if it already exists; "
            "no file is ever created implicitly)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # The authorization decision is made before settings are touched, so an
    # unauthorized run cannot even read the environment.
    if not args.authorize_live_verification:
        print(
            f"{_PREFIX}: BLOCKED — AUTHORIZATION REQUIRED: live verification "
            "requires --authorize-live-verification and separate owner approval",
            file=sys.stderr,
        )
        return EXIT_CODES["BLOCKED — AUTHORIZATION REQUIRED"]

    try:
        from src.s8.verification import S8Request, verify_schema

        settings = _settings()
        request = S8Request(
            authorized=True,
            application_dsn=settings.database_url,
            designated_dsn=args.target,
            test_dsn=settings.test_database_url,
        )
        report = verify_schema(request)
    except Exception:  # noqa: BLE001 - never a traceback on the console
        print(f"{_PREFIX}: unexpected internal error", file=sys.stderr)
        return 1

    document = report.to_dict()
    if args.report:
        target_path = Path(args.report)
        if target_path.exists():
            print(
                f"{_PREFIX}: refusing to overwrite an existing report file",
                file=sys.stderr,
            )
            return 1
        target_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(document, sort_keys=True))
    else:
        print(f"outcome: {report.outcome}")
        print(f"contract: {document['contract_version']}")
        print(f"target: {document['target'].get('dbname')}")
        print(f"server: {document['server'].get('version')}")
        print(f"g2: {document['g2'].get('status')}")
        print(f"g8: {document['g8'].get('status')}")
        if report.reasons:
            print(f"reasons: {', '.join(report.reasons)}")
        print(f"limitation: {document['concurrency_limitation']}")
    return EXIT_CODES.get(report.outcome, 1)


if __name__ == "__main__":
    raise SystemExit(main())
