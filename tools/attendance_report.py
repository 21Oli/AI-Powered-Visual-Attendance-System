"""Read-only attendance report CLI (Phase 2 — P2-4, report tool only).

Inspects the durable attendance journal written by the P2-1 store
(``<DATA_DIR>/attendance/events.jsonl``) through its **existing**
``AttendanceEventStore.load_events()`` and existing event model — the
CLI adds zero decision logic, zero journal-schema knowledge, and zero
storage behavior of its own.

Strictly read-only: the tool never appends, truncates, repairs,
rewrites, locks, or otherwise modifies the journal. Corruption,
unsupported schema versions, and any other store-load error are
reported with the store's own typed messages and a distinct exit code
— a failed load is never presented as an empty report.

**Not wired into the live attendance workflow** (the live demo remains
unconnected to persistence, unchanged); this is an operator inspection
utility that runs only on demand.

Output is deterministic: fixed columns, stable chronological default
ordering (``(occurred_at, original_index)`` — equal timestamps resolve
by journal position, mirroring the project's documented tie-break), an
explicit ``--journal-order`` alternative, and verbatim event fields
(``"007"`` stays ``"007"``; duplicate ``event_id`` records are printed
as-is, never de-duplicated or warned about).

Date filtering uses the journal's UTC timestamps only, as half-open
intervals — timestamps are UTC-normalized by the event model, so
``--since`` is inclusive at that date's 00:00:00Z, ``--until`` covers
that whole date (ending at the *next* midnight, exclusive), and
``--day`` is both together. Site-local timezones are intentionally not
supported in this first version (documented limitation).

Exit codes (deliberate contract, argparse's default exit overridden):

* ``0`` — success, including an empty journal and an incomplete
  trailing record (torn tail: valid rows print, a warning goes to
  stderr, the loader's exclude-the-fragment decision is surfaced but
  never repaired).
* ``2`` — journal read failure (corrupt journal / unsupported schema
  version / unreadable file): the typed error is reported on stderr
  and NO event rows are printed (a failed load is never presented as
  an empty report).
* ``3`` — invalid arguments or dates: checked before any journal
  access. ``--help`` behaves normally (prints help, exit 0) via the
  untouched default argparse path.

Privacy: output carries exactly the four journal schema fields (UTC
timestamp, event type, employee ID verbatim, event ID verbatim); no
biometric data exists in the journal to print, and nothing is logged
or exported.

No new dependencies; stdlib only. Importing this module has **no
filesystem side effects and loads no ``src`` modules at all** — the
store/attention imports are deliberately deferred into :func:`main`
so that a plain ``import`` of this file stays biometric-free (the
store's module graph transitively includes recognition-layer
types). Running the report does import the P2-1 store (necessary to
reuse its loader unchanged), which loads no camera device, no model,
and no biometric component — asserted by tests — and no camera or
model code is ever executed by this CLI.

Examples::

    python tools/attendance_report.py
    python tools/attendance_report.py --employee EMP-001 --since 2026-10-23
    python tools/attendance_report.py --day 2026-10-23 --summary
    python tools/attendance_report.py --journal-order
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

# Make "src" importable when invoked as a script from the repo root
# (mirrors the other tools/ scripts and pytest.ini's pythonpath = .).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if TYPE_CHECKING:  # annotation-only; loaded lazily inside main()
    from src.attendance import AttendanceEvent

_EXIT_OK = 0
_EXIT_JOURNAL_ERROR = 2
_EXIT_USAGE_ERROR = 3

#: Strict ``YYYY-MM-DD`` shape: strptime alone would accept unpadded
#: months/days, which would make two spellings of one date pass
#: silently; the report contract wants one canonical spelling.
_DATE_SHAPE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


class _ArgumentUsageError(ValueError):
    """Invalid arguments or dates (CLI usage error; exit code 3)."""


class _UsageParser(argparse.ArgumentParser):
    """``ArgumentParser`` whose errors become exit-code 3 instead of 2.

    Only ``error()`` is overridden — the ``--help`` path (``exit()``
    with code 0) still raises the ordinary ``SystemExit(0)`` the
    default argparse behavior provides, so help behaves normally.
    """

    def error(self, message: str) -> None:
        raise _ArgumentUsageError(message)


def _parse_utc_date(raw: str) -> datetime:
    """Convert a ``YYYY-MM-DD`` string to that date's 00:00:00 UTC.

    Raises:
        argparse.ArgumentTypeError: malformed shape or an impossible
            calendar date — never a journal access (validation runs
            during argument parsing, before any store is constructed).
    """
    if not _DATE_SHAPE.match(raw):
        raise argparse.ArgumentTypeError(
            f"date must be exactly YYYY-MM-DD (UTC), got {raw!r}"
        )
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"date {raw!r} is not a valid calendar date (YYYY-MM-DD, UTC)"
        ) from None
    return parsed.replace(tzinfo=UTC)


def _midnight_after(date_utc: datetime) -> datetime:
    """Exclusive half-open end bound: next day's 00:00:00 UTC."""
    return date_utc + timedelta(days=1)


def _build_parser() -> argparse.ArgumentParser:
    parser = _UsageParser(
        prog="attendance_report",
        description=(
            "Read-only inspection of the durable attendance journal "
            "(<DATA_DIR>/attendance/events.jsonl). Lists events, "
            "filters them, or summarizes check-ins per employee and "
            "day (plus a per-employee/day check-out table when the "
            "selection contains check-outs). Never modifies the "
            "journal; report is not wired into the live attendance "
            "workflow."
        ),
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        default=None,
        help=(
            "Project data directory holding attendance/events.jsonl. "
            "Default: the application's configured DATA_DIR."
        ),
    )
    parser.add_argument(
        "--employee",
        action="append",
        default=None,
        metavar="ID",
        help=(
            "Exact employee ID filter (verbatim match, ID is never "
            "normalized). Repeatable for multiple employees."
        ),
    )
    parser.add_argument(
        "--since",
        type=_parse_utc_date,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Include events on/after this UTC date (inclusive, "
            "00:00:00Z). UTC only."
        ),
    )
    parser.add_argument(
        "--until",
        type=_parse_utc_date,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Include events within this whole UTC date (ends at the "
            "next midnight, exclusive — fractional seconds on that "
            "date are fully covered). UTC only."
        ),
    )
    parser.add_argument(
        "--day",
        type=_parse_utc_date,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Single UTC day: that midnight through the next midnight "
            "(exclusive). Exclusive with --since/--until. UTC only."
        ),
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help=(
            "Print per-employee x per-day check-in counts over the "
            "filtered events (chronological day order, then verbatim "
            "employee ID) instead of the event listing."
        ),
    )
    parser.add_argument(
        "--journal-order",
        action="store_true",
        help=(
            "Print events in journal (append) order instead of the "
            "default chronological order (occurred_at, then journal "
            "position for equal timestamps)."
        ),
    )
    return parser


def _validate_interactions(args: argparse.Namespace) -> None:
    """Reject option combinations that make no sense — before any I/O."""
    if args.day is not None and (args.since is not None or args.until is not None):
        raise _ArgumentUsageError(
            "--day cannot be combined with --since or --until "
            "(use --day alone for a single UTC day)"
        )
    if args.since is not None and args.until is not None:
        if _midnight_after(args.until) <= args.since:
            raise _ArgumentUsageError(
                "--until date precedes --since date: the filtered "
                "interval would be empty; use consistent dates"
            )


def _resolve_interval(
    args: argparse.Namespace,
) -> tuple[datetime | None, datetime | None]:
    """Half-open ``[start, end)`` UTC interval from the date flags."""
    if args.day is not None:
        return args.day, _midnight_after(args.day)
    start = args.since if args.since is not None else None
    end = _midnight_after(args.until) if args.until is not None else None
    return start, end


def _format_timestamp(event: AttendanceEvent) -> str:
    """``YYYY-MM-DD HH:MM:SSZ`` — the event is already UTC-normalized."""
    return event.occurred_at.strftime("%Y-%m-%d %H:%M:%SZ")


def _chosen_interval(
    start: datetime | None, end: datetime | None
) -> str:
    """Deterministic human echo of the applied half-open interval."""
    pieces: list[str] = []
    if start is not None:
        pieces.append(f"[{start.strftime('%Y-%m-%d %H:%M:%SZ')}, ")
    else:
        pieces.append("[earliest, ")
    if end is not None:
        pieces.append(end.strftime("%Y-%m-%d %H:%M:%SZ"))
    else:
        pieces.append("latest")
    pieces.append(")")
    return "".join(pieces)


def _filter_events(
    events: Sequence[AttendanceEvent],
    *,
    employees: frozenset[str] | None,
    start: datetime | None,
    end: datetime | None,
) -> tuple[tuple[AttendanceEvent, int], ...]:
    """Filtered ``(event, original_index)`` pairs, journal order in."""
    kept: list[tuple[AttendanceEvent, int]] = []
    for index, event in enumerate(events):
        if employees is not None and event.employee_id not in employees:
            continue
        if start is not None and event.occurred_at < start:
            continue
        if end is not None and event.occurred_at >= end:
            continue
        kept.append((event, index))
    return tuple(kept)


def _ordered(
    filtered: Sequence[tuple[AttendanceEvent, int]],
    *,
    journal_order: bool,
) -> tuple[tuple[AttendanceEvent, int], ...]:
    if journal_order:
        return tuple(filtered)
    # Stable chronological default: equal timestamps resolve by
    # original journal position (the project's documented tie-break).
    return tuple(
        sorted(filtered, key=lambda pair: (pair[0].occurred_at, pair[1]))
    )


def _print_listing(
    rows: Sequence[tuple[AttendanceEvent, int]],
) -> None:
    """Fixed-width listing: TIME (UTC) | EVENT | EMPLOYEE | EVENT_ID."""
    headers = ("TIME (UTC)", "EVENT", "EMPLOYEE", "EVENT_ID")
    cells = [
        (
            _format_timestamp(event),
            event.event_type.value,
            event.employee_id,
            event.event_id,
        )
        for event, _index in rows
    ]
    widths = [
        max([len(header)] + [len(col) for col in column])
        for header, column in zip(headers, zip(*cells), strict=True)
    ] if cells else [len(header) for header in headers]
    print(
        "  ".join(
            header.ljust(width) for header, width in zip(headers, widths)
        ).rstrip()
    )
    for row in cells:
        line = "  ".join(
            value.ljust(width) for value, width in zip(row, widths)
        ).rstrip()
        print(line)


def _print_summary(
    filtered: Sequence[tuple[AttendanceEvent, int]],
) -> None:
    """Per-employee x per-day counts: day order, then verbatim ID.

    P2-6 S5: the event type is never misclassified — ``CHECK_IN``
    events feed the ``CHECK_INS`` table only, ``CHECK_OUT`` events
    feed the additive ``CHECK_OUTS`` table only (printed when the
    filtered selection contains at least one check-out). Each table
    prints its header only when non-empty, so pre-S2 (check-in-only)
    history keeps the byte-identical legacy summary output.
    """
    # Deferred into the same lazy block as main()'s store import: no
    # new import-time behavior, the module graph is already loaded.
    from src.attendance import EventType

    checkins: dict[tuple[str, str], int] = {}
    checkouts: dict[tuple[str, str], int] = {}
    for event, _index in filtered:
        key = (
            event.occurred_at.strftime("%Y-%m-%d"),
            event.employee_id,
        )
        if event.event_type is EventType.CHECK_IN:
            checkins[key] = checkins.get(key, 0) + 1
        elif event.event_type is EventType.CHECK_OUT:
            checkouts[key] = checkouts.get(key, 0) + 1
    if checkins:
        print("DAY", "EMPLOYEE", "CHECK_INS")
        for (day, employee_id), count in sorted(checkins.items()):
            print(day, employee_id, count)
    if checkouts:
        print("DAY", "EMPLOYEE", "CHECK_OUTS")
        for (day, employee_id), count in sorted(checkouts.items()):
            print(day, employee_id, count)


def _print_filters_echo(
    *,
    employees: frozenset[str] | None,
    start: datetime | None,
    end: datetime | None,
    journal_order: bool,
) -> None:
    parts: list[str] = []
    if employees is not None:
        parts.append("employee=" + ",".join(sorted(employees)))
    parts.append(f"interval={_chosen_interval(start, end)}")
    parts.append(
        "order=journal" if journal_order else "order=chronological"
    )
    print(f"filters: {' '.join(parts)}")


def main(argv: list[str] | None = None) -> int:
    """Run the report; return the documented exit code.

    ``--help`` prints help text and returns 0 (the default argparse
    ``SystemExit(0)`` path, untouched). Invalid arguments/dates return
    3 before any journal access. Journal read failures return 2 with
    the store's typed error on stderr and no event rows.

    The ``src`` imports are deferred to this call (not module level)
    so importing this module loads no biometric components; executing
    the report imports the P2-1 store only and never initializes a
camera, model, or biometric pipeline.
    """
    # Deferred: reuse the unchanged P2-1 store loader and event model.
    from src.attendance_store import (
        AttendanceEventStore,
        AttendanceStoreError,
    )
    from src.config import AppSettings, SETTINGS

    try:
        args = _build_parser().parse_args(argv)
        _validate_interactions(args)
    except _ArgumentUsageError as exc:
        print(f"attendance-report: error: {exc}", file=sys.stderr)
        return _EXIT_USAGE_ERROR
    except SystemExit as exc:
        # Only --help (and orthogonal default exits) reach here because
        # error() was overridden to a typed exception instead.
        return 0 if exc.code in (None, 0) else int(exc.code)

    data_dir = (
        Path(args.data_dir) if args.data_dir is not None else SETTINGS.data_dir
    )
    store = AttendanceEventStore(settings=AppSettings(data_dir=data_dir))

    try:
        load_result = store.load_events()
    except AttendanceStoreError as exc:
        # Corrupt journal / unsupported schema / unreadable data: the
        # store's own typed message is the report; NEVER an empty
        # result and never a repair attempt.
        print(
            f"attendance-report: journal error: {exc}", file=sys.stderr
        )
        return _EXIT_JOURNAL_ERROR

    start, end = _resolve_interval(args)
    employees = (
        frozenset(args.employee) if args.employee is not None else None
    )
    filtered = _filter_events(
        load_result.events,
        employees=employees,
        start=start,
        end=end,
    )

    if not load_result.events:
        print(f"No attendance events found in {store.journal_path}")
        _print_filters_echo(
            employees=employees,
            start=start,
            end=end,
            journal_order=args.journal_order,
        )
        return _EXIT_OK

    if not filtered:
        print("No attendance events matched the given filters.")
        _print_filters_echo(
            employees=employees,
            start=start,
            end=end,
            journal_order=args.journal_order,
        )
        return _EXIT_OK

    print(
        f"warning: journal ends in an incomplete trailing record "
        "(torn append); that fragment was excluded by the loader",
        file=sys.stderr,
    ) if load_result.incomplete_trailing_record else None

    rows = _ordered(filtered, journal_order=args.journal_order)
    if args.summary:
        _print_summary(rows)
    else:
        _print_listing(rows)
    distinct_employees = len({event.employee_id for event, _ in rows})
    _print_filters_echo(
        employees=employees,
        start=start,
        end=end,
        journal_order=args.journal_order,
    )
    print(
        f"{len(rows)} events ({distinct_employees} employees)"
    )
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
