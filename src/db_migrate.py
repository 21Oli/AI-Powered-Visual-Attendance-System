"""Schema migration runner core for the Phase 3 PostgreSQL backend
(Step 1.5b) — **offline module**: no driver import, no connection, no
database access anywhere in this module.

This module implements the approved migration contract recorded in
``DB_DESIGN.md`` (Step 1.5a) after the owner's OQ-1..7 decisions:

* **OQ-1 — advisory-lock contention discipline (MECHANISM SUPERSEDED by
  the owner: **Option B**).** Before touching any migration state the
  runner asks the executor to acquire a **session-scoped, nonblocking**
  lock — ``pg_try_advisory_lock`` on a fixed, documented key
  (:data:`ADVISORY_LOCK_KEY`) — held on a **dedicated connection the
  executor owns and keeps open for the entire run**. Failure →
  immediate typed refusal (:class:`MigrationContentionError`); the
  runner never *infers* concurrency from ledger/state appearance (no
  "looks idle" heuristics), never waits, and never retries. The runner
  releases the lock — and the executor closes that dedicated connection
  — from a single ``finally`` path covering success, every typed
  failure, every unexpected exception, and a failed acquisition.

  *History (preserved):* OQ-1 originally recorded a **transaction-scoped**
  ``pg_try_advisory_xact_lock``. The independent audit (Step 1.5b
  follow-up, finding H-3) showed that a lock scoped to the transaction
  that took it is released *before* the per-migration transactions it is
  meant to protect. The owner approved **Option B**, superseding **only
  the lock mechanism**: the per-migration transaction and ledger
  semantics below are unchanged, and Option A (one outer transaction)
  was rejected precisely because it would have revoked them. If the
  dedicated lock connection itself dies mid-run, PostgreSQL releases the
  session lock and the run aborts — there is no re-acquire and no
  recovery attempt. See ``DB_DESIGN.md`` Section 3.6 and
  ``DEVELOPMENT_STATUS.md`` Step 31.
* **OQ-2 — one ledger row per applied migration.** The
  ``schema_version`` table records ``(version, checksum, applied_at)``
  per applied migration. Previously applied checksums are re-verified
  against the current script contents on every run; a modified applied
  migration is an error, never a silent skip.
* **OQ-3 — forward-only, backup-restore recovery.** No down-migrations
  exist anywhere in this contract; operational recovery is restore from
  a verified full-database backup taken before the migration window
  (documented runbook prerequisite, owner-side).
* **OQ-4 — numbered plain-SQL files.** Migrations are ``V<N>__<name>.sql``
  files; the runner refuses filenames it cannot parse, duplicate
  versions, and the prohibited statements ``CREATE DATABASE`` /
  ``CREATE [UNIQUE] INDEX CONCURRENTLY`` (none is transactional, so none
  may appear inside the runner's migration transaction — the ``UNIQUE``
  form is equally non-transactional and is matched too, see
  :func:`_validate_script_text`). Statement
  execution happens **per file, inside one transaction** — the runner
  passes the whole file's SQL text to the executor's
  :meth:`MigrationExecutor.apply_migration` once; the transactional
  statement-by-statement accounting is the real executor's documented
  responsibility (psycopg runs a multi-statement string statement by
  statement on the server inside the open transaction).

Database connection semantics (driver import, commit/rollback, redacted
error reporting, blank-DSN refusal) belong to the connection layer
(:mod:`src.db`) and the *runtime wiring step* — this module is
deliberately driver-agnostic so every behavior above is testable
offline with fake executors, exactly like the rest of the repository's
suites. **No real database may be touched by anything here.**

History structure:

* the ledger is materialized by the runner itself
  (``ensure_ledger``), never by versioned migration files — the
  bootstrap of the migration system's own bookkeeping must not race,
  or double-apply against, a versioned migration's content;
* each migration file is applied at most once; applied scripts are
  never re-runnable;
* the recorded version advances only atomically with the DDL that
  earned it (executor-side single-transaction contract).

Error family (typed, sanitized): messages are static strings that may
carry migration *version numbers* and error *reasons*, never file
paths, file contents, DSN values, credentials, or driver exception
text. The underlying failure is preserved as ``__cause__`` for trusted,
server-side diagnostics only (same redaction-discipline boundary the
rest of the backend already uses).
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Sequence

from src.config import PROJECT_ROOT

__all__ = [
    "ADVISORY_LOCK_KEY",
    "DEFAULT_MIGRATIONS_DIR",
    "LEDGER_TABLE",
    "ApplyOutcome",
    "MigrationChecksumError",
    "MigrationContentionError",
    "MigrationApplicationError",
    "MigrationCleanupError",
    "MigrationError",
    "MigrationExecutor",
    "MigrationFileError",
    "MigrationGapError",
    "MigrationIntegrityError",
    "MigrationMissingScriptError",
    "MigrationScript",
    "load_migrations",
    "migrate",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Directory holding the versioned migration SQL files.
DEFAULT_MIGRATIONS_DIR: Final[Path] = PROJECT_ROOT / "migrations"

#: Migration SQL files are named ``V<N>__<name>.sql`` with N >= 1. The
#: version is the integer in the file name — the system's ordering key.
#: (``<name>`` is descriptive only; two files may not share a version.)
_MIGRATION_FILE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^V(\d+)__([A-Za-z0-9_]+)\.sql$"
)

#: The migration ledger: one row per applied migration (approved OQ-2),
#: written inside each migration's own transaction by the executor.
#: Materialized by the runner (:meth:`MigrationExecutor.ensure_ledger`),
#: never by a versioned migration file (docstring rationale).
LEDGER_TABLE: Final[str] = "schema_version"

#: Fixed, documented key for the migration runner's advisory lock
#: (approved OQ-1 policy). The value is an arbitrary but permanent
#: constant reserved to *this system's* migration runner; no other
#: subsystem may use it — do not make it configurable.
#:
#: Acquired **once, before any state check**, with the **session-scoped**
#: nonblocking ``pg_try_advisory_lock`` (owner-approved Option B, which
#: supersedes the original transaction-scoped ``pg_try_advisory_xact_lock``
#: mechanism only — audit finding H-3). Because it is session-scoped it
#: survives the COMMIT of each per-migration transaction, which is
#: exactly what the run needs; PostgreSQL releases it when the dedicated
#: lock connection closes, so a crashed runner leaks nothing.
ADVISORY_LOCK_KEY: Final[int] = 0x50484153453  # "PHASE3" mnemonic

#: Prohibited inside migration SQL (approved OQ-4): non-transactional
#: statements would break the commit-atomically-with-the-ledger guarantee.
#: ``CREATE [UNIQUE] INDEX CONCURRENTLY`` is matched in both forms: the
#: ``UNIQUE`` qualifier does not make it transactional, and matching only
#: the bare form was a real bypass found in audit (Step 1.5b follow-up).
#: Matching runs on *normalized* text (comments removed, whitespace runs
#: collapsed), so case, newlines, and interleaved comments cannot evade it.
_PROHIBITED_STATEMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\bcreate\s+(?:database|(?:unique\s+)?index\s+concurrently)\b",
    re.IGNORECASE,
)

#: ``-- ...`` line comments and ``/* ... */`` block comments are removed
#: before the prohibited-statement scan. The block form is matched
#: non-greedily and **not** nested (PostgreSQL nests block comments): a
#: nested comment therefore keeps its inner text, which can only make the
#: scanner *more* conservative (an over-refusal), never more permissive.
#: This is deliberately a comment stripper, **not** a SQL parser: string
#: literals are not special-cased either, so a literal containing
#: "create database" is also refused (safe direction).
_LINE_COMMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)
_WHITESPACE_RUN_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Canonical line ending for checksums (M-4): CRLF and lone CR both hash
#: identically to LF, so a checkout's line-ending conversion (this repo
#: runs ``core.autocrlf=true``) cannot fake a "modified migration".
_CANONICAL_NEWLINE: Final[str] = "\n"


# ---------------------------------------------------------------------------
# Script model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationScript:
    """A validated migration file's derived values (no filesystem handle)."""

    version: int
    #: Descriptive file name from the approved ``V<N>__<name>.sql`` shape.
    name: str
    sql_text: str
    #: SHA-256 hex digest of the file's **canonical text** (OQ-2 ledger
    #: checksum): UTF-8 without a byte-order mark, every line ending
    #: normalized to LF, hashed as UTF-8 bytes. Line-ending conversion
    #: alone therefore never changes the checksum; any change to the SQL
    #: content does.
    checksum: str


# ---------------------------------------------------------------------------
# Typed, sanitized error family
# ---------------------------------------------------------------------------


class MigrationError(RuntimeError):
    """Base for every migration failure (typed family, sanitized text)."""


class MigrationFileError(MigrationError):
    """A migration file violates the load contract (name, version,
    duplicate, or prohibited statement). Raised before any executor
    call — filesystem validation never touches the database."""


class MigrationContentionError(MigrationError):
    """Another migration runner holds the advisory key (approved OQ-1:
    fail fast, never wait, never infer). Raised *before* any migration
    state is read or written."""


class MigrationCleanupError(MigrationError):
    """Releasing the advisory lock and closing the dedicated lock
    connection failed **and no other failure was propagating**. The
    migration work itself may have completed: the run's outcome is
    unknown, and the session lock may still be held by a live
    connection until it closes. When another failure *is* propagating,
    the release failure is attached to it as a note instead, so it never
    masks the original cause."""


class MigrationIntegrityError(MigrationError):
    """The database's migration state is inconsistent with the ledger
    (e.g. the employee table exists without a ledger row) — reported
    and refused, never silently repaired or re-derived."""


class MigrationChecksumError(MigrationError):
    """An already-applied migration's file contents changed since it was
    applied (ledger checksum no longer matches) — refused, never
    silently skipped."""


class MigrationMissingScriptError(MigrationError):
    """The ledger records an applied migration whose script no longer
    exists in the migration directory."""


class MigrationGapError(MigrationError):
    """Pending migrations do not continue the applied sequence exactly
    (a gap, an out-of-order start, or a non-monotonic file set)."""


class MigrationApplicationError(MigrationError):
    """A migration's execution raised inside the executor. The runner
    aborts (no further migrations, no ledger change beyond the failed
    transaction's rollback); the message names only the version and the
    underlying exception *type* — never driver text or file content."""


# ---------------------------------------------------------------------------
# Executor contract (the only database-facing seam)
# ---------------------------------------------------------------------------


class MigrationExecutor(ABC):
    """The runner's single seam to a database connection.

    The real psycopg-backed executor is the *runtime wiring step's*
    responsibility; the 1.5b gate defines and tests this contract with
    fakes only. Implementations must honor the redaction discipline of
    :mod:`src.db` and never surface raw driver text through these calls'
    exceptions in user-facing contexts.

    Contract notes for implementors:

    * :meth:`apply_migration` must execute ``sql_text`` and insert the
      ``(version, checksum, applied_at)`` ledger row **inside one
      transaction**, committing atomically; on any failure the
      transaction rolls back and the ledger is unchanged. One migration
      per transaction, never one transaction around the whole run (a
      later failure must not undo migrations that already committed).
    * **Lock ownership (approved Option B).** The *executor* owns one
      **dedicated lock connection**; the *runner* owns its lifecycle. The
      runner calls :meth:`acquire_advisory_lock` exactly once before any
      state check and :meth:`release_advisory_lock` exactly once in a
      ``finally`` path. The dedicated connection must stay open for the
      whole run — it may carry the migration work or sit idle beside it,
      but it must not be closed, reset, or repurposed before release,
      because closing it silently drops the session lock.
    * The runner performs no connection lifetime management beyond those
      two calls: it never opens, closes, inspects, or retries a
      connection, and never re-acquires a lock once acquired or lost.
    * Implementations must honor the redaction discipline of
      :mod:`src.db` and never surface raw driver text through these
      calls' exceptions in user-facing contexts.
    """

    @abstractmethod
    def acquire_advisory_lock(self) -> bool:
        """Acquire ``pg_try_advisory_lock(ADVISORY_LOCK_KEY)`` on the
        executor's dedicated lock connection (session-scoped,
        nonblocking — approved Option B).

        Returns ``True`` when the session now holds the lock; ``False``
        when another runner holds it, in which case the executor must
        leave no connection open and no lock held. Must never block,
        wait, retry, or sleep.
        """

    @abstractmethod
    def release_advisory_lock(self) -> None:
        """Release ``ADVISORY_LOCK_KEY`` and **close** the dedicated
        lock connection (approved Option B lifecycle).

        Called exactly once per :func:`migrate` attempt, from a
        ``finally`` path, including attempts where acquisition returned
        ``False`` or raised and where the run failed. It must therefore
        be **safe and silent when there is nothing to release** — never
        acquired, not held, connection never opened, or already closed —
        and **idempotent**: a second call, or a call after closure, is a
        no-op rather than an error, so cleanup can never double-release
        or use a closed connection.

        A genuine driver failure *may* be raised; the runner reports it
        without masking any other failure (see
        :class:`MigrationCleanupError`).
        """

    @abstractmethod
    def ensure_ledger(self) -> None:
        """Create the ledger table if absent (runner-owned bookkeeping;
        static DDL; no-ops when it already exists)."""

    @abstractmethod
    def list_tables(self) -> frozenset[str]:
        """Return the names of existing user tables (read-only)."""

    @abstractmethod
    def fetch_applied(self) -> Sequence[tuple[int, str]]:
        """Return ``(version, checksum)`` rows currently recorded in the
        ledger (read-only)."""

    @abstractmethod
    def apply_migration(
        self, version: int, sql_text: str, checksum: str, applied_at: datetime
    ) -> None:
        """Atomically run one migration's SQL and record its ledger row."""


# ---------------------------------------------------------------------------
# Loading and validation (pure filesystem; runs before any executor call)
# ---------------------------------------------------------------------------


def _normalized_for_scan(sql_text: str) -> str:
    """Comment-stripped, whitespace-collapsed view used only by the
    prohibited-statement scanner.

    Removes ``--`` line comments and ``/* ... */`` block comments, then
    collapses every whitespace run (including newlines) to one space, so
    ``create /*x*/ index``, ``CREATE\vINDEX`` and ``create\n  index`` all
    normalize to the same shape before matching.

    Known and accepted limitations (all fail in the *refusal* direction):
    block comments are not nested, and string literals are not
    distinguished from SQL text.
    """
    without_block = _BLOCK_COMMENT_PATTERN.sub(" ", sql_text)
    without_line = _LINE_COMMENT_PATTERN.sub("", without_block)
    return _WHITESPACE_RUN_PATTERN.sub(" ", without_line)


def _canonical_sql_text(sql_text: str) -> str:
    """Canonical text for checksumming (M-4).

    A leading byte-order mark is dropped and CRLF / lone-CR line endings
    become LF. Nothing else is touched: the digest stays sensitive to
    every meaningful SQL change (spacing inside a line included).
    """
    text = sql_text.lstrip("\ufeff")
    return text.replace("\r\n", _CANONICAL_NEWLINE).replace(
        "\r", _CANONICAL_NEWLINE
    )


def _checksum_for(sql_text: str) -> str:
    """SHA-256 of the canonical text, as UTF-8 bytes (M-4 contract)."""
    return hashlib.sha256(
        _canonical_sql_text(sql_text).encode("utf-8")
    ).hexdigest()


def _validate_script_text(sql_text: str, version: int) -> None:
    """Refuse prohibited statements (OQ-4) with a sanitized message."""
    if _PROHIBITED_STATEMENT_PATTERN.search(_normalized_for_scan(sql_text)):
        raise MigrationFileError(
            f"migration {version} contains a prohibited non-transactional "
            "statement (CREATE DATABASE and CREATE [UNIQUE] INDEX "
            "CONCURRENTLY are never allowed in migration files)"
        )


def load_migrations(
    migrations_dir: Path | None = None,
) -> list[MigrationScript]:
    """Discover and validate migration files, ascending by version.

    Raises ``MigrationFileError`` (before any database interaction) for:
    unparseable ``.sql`` file names, version ``0`` or negative-scale
    names, duplicate version numbers, or prohibited statements. An
    absent or empty directory is valid — it simply yields no scripts.
    """
    directory = migrations_dir if migrations_dir is not None else DEFAULT_MIGRATIONS_DIR
    if not directory.is_dir():
        return []

    seen: dict[int, MigrationScript] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".sql":
            continue  # non-SQL files are not migrations; ignore silently
        match = _MIGRATION_FILE_PATTERN.match(path.name)
        if match is None:
            raise MigrationFileError(
                f"unrecognized migration file name {path.name!r} "
                "(expected V<N>__<name>.sql with N >= 1)"
            )
        version = int(match.group(1))
        if version < 1:
            raise MigrationFileError(
                f"migration version must be >= 1, got {version} "
                f"({path.name!r})"
            )
        sql_text = path.read_text(encoding="utf-8")
        _validate_script_text(sql_text, version)
        script = MigrationScript(
            version=version,
            name=path.name,
            sql_text=sql_text,
            checksum=_checksum_for(sql_text),
        )
        if version in seen:
            raise MigrationFileError(
                f"duplicate migration version {version} "
                f"({path.name!r} and {seen[version].name!r})"
            )
        seen[version] = script
    return [seen[version] for version in sorted(seen)]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyOutcome:
    """What one runner pass did (pure data; safe to log — no paths, no
    DSN, no SQL text)."""

    previous_version: int
    applied_versions: tuple[int, ...]
    current_version: int


def migrate(
    executor: MigrationExecutor,
    migrations_dir: Path | None = None,
    *,
    now: datetime | None = None,
) -> ApplyOutcome:
    """Apply every pending migration in ascending order.

    Sequence (approved OQ-1..OQ-4 contract; OQ-1 mechanism = Option B):

    1. Load and validate files (all filesystem-contract violations
       fail *before* the executor is touched).
    2. Acquire the **session-scoped** advisory lock on the executor's
       dedicated connection — the very first executor call. Failure is
       a typed refusal, and nothing (ledger creation included) is read
       or written.
    3. Ensure the ledger, then reconcile ledger vs files: modified
       applied migrations and missing scripts are errors; inconsistent
       ledger rows are integrity errors; ``employee`` without a ledger
       row is an integrity error (never silently re-derived).
    4. Apply pending versions — strictly continuing highest-applied + 1
       — each inside its own executor-managed transaction; a failure at
       any version aborts the run (later versions are never attempted,
       earlier committed versions stay committed).
    5. Release the lock and close the dedicated connection in a single
       ``finally`` path, whatever happened in 2–4. A release failure
       never replaces the original exception.

    The lock is session-scoped, so it stays held across the state checks
    and across every per-migration COMMIT: contention is excluded for
    the whole run, not merely for one transaction.
    """
    scripts = load_migrations(migrations_dir)

    applied_at = now if now is not None else datetime.now(UTC)
    if applied_at.tzinfo is None or applied_at.utcoffset() is None:
        raise ValueError(
            "now must be timezone-aware (e.g. datetime.now(UTC)), got naive "
            f"datetime {applied_at!r}"
        )

    primary_failure: BaseException | None = None
    try:
        if not executor.acquire_advisory_lock():
            raise MigrationContentionError(
                "another migration run holds the advisory lock; refusing to "
                "start (fail-fast, by contract)"
            )

        executor.ensure_ledger()
        tables = executor.list_tables()
        applied = executor.fetch_applied()

        _reconcile(applied, tables, scripts)

        previous_version = max((version for version, _ in applied), default=0)

        pending: list[MigrationScript] = []
        expected = previous_version + 1
        for script in scripts:
            if script.version in {version for version, _ in applied}:
                continue
            if script.version != expected:
                raise MigrationGapError(
                    f"migration sequence has a gap: expected version {expected} "
                    f"next, found {script.version}"
                )
            pending.append(script)
            expected += 1

        for script in pending:
            try:
                executor.apply_migration(
                    version=script.version,
                    sql_text=script.sql_text,
                    checksum=script.checksum,
                    applied_at=applied_at,
                )
            except Exception as exc:
                raise MigrationApplicationError(
                    f"migration {script.version} failed and its transaction "
                    f"rolled back; refusing to continue (underlying exception "
                    f"type: {type(exc).__name__})"
                ) from exc

        return ApplyOutcome(
            previous_version=previous_version,
            applied_versions=tuple(script.version for script in pending),
            current_version=previous_version + len(pending),
        )
    except BaseException as exc:
        # Record what is propagating so the cleanup path can attach a
        # release failure to it instead of masking it. The exception is
        # immediately re-raised unchanged: no translation, no wrapping.
        primary_failure = exc
        raise
    finally:
        cleanup_error = _release_advisory_lock(executor)
        if cleanup_error is not None:
            if primary_failure is not None:
                primary_failure.add_note(
                    "advisory-lock cleanup also failed "
                    f"({type(cleanup_error).__name__}); the session lock "
                    "may still be held by the dedicated connection"
                )
            else:
                raise MigrationCleanupError(
                    "the advisory lock could not be released after a "
                    "migration run that otherwise completed "
                    f"(underlying exception type: {type(cleanup_error).__name__})"
                ) from cleanup_error


def _release_advisory_lock(executor: MigrationExecutor) -> Exception | None:
    """Attempt the approved-Option-B cleanup exactly once.

    Returns the cleanup exception, or ``None`` when release succeeded.
    Never raises, and never retries: the caller decides how to report a
    cleanup failure (a note on a propagating failure, or a typed
    :class:`MigrationCleanupError` when nothing else failed).

    The executor's release is contractually idempotent and no-op safe,
    which is what makes it safe to call on every path — including when
    acquisition returned ``False``, when acquisition itself raised, and
    when the run never reached a state check.
    """
    try:
        executor.release_advisory_lock()
    except Exception as exc:
        return exc
    return None


def _reconcile(
    applied: Sequence[tuple[int, str]],
    tables: frozenset[str],
    scripts: list[MigrationScript],
) -> None:
    """Verify ledger-vs-files consistency (refuse, never self-repair)."""
    rows = list(applied)
    script_by_version = {script.version: script for script in scripts}

    seen_versions: set[int] = set()
    for version, checksum in rows:
        if version in seen_versions:
            raise MigrationIntegrityError(
                f"ledger records version {version} more than once "
                "(inconsistent ledger state)"
            )
        seen_versions.add(version)
        script = script_by_version.get(version)
        if script is None:
            raise MigrationMissingScriptError(
                f"ledger records applied version {version} but no "
                "migration file provides it"
            )
        if checksum != script.checksum:
            raise MigrationChecksumError(
                f"migration {version}'s file contents changed since it was "
                "applied (checksum mismatch); refusing to run"
            )

    sorted_versions = sorted(seen_versions)
    for earlier, later in zip(sorted_versions, sorted_versions[1:]):
        if later != earlier + 1:
            raise MigrationIntegrityError(
                f"ledger records non-contiguous applied versions "
                f"({earlier} then {later}; missing {earlier + 1})"
            )

    if not rows:
        # The employee table without its ledger row would mean the
        # schema was created outside this runner: refuse, never
        # re-derive the version (DB_DESIGN.md Section 3.1).
        if "employee" in tables:
            raise MigrationIntegrityError(
                "employee table exists but the migration ledger records "
                "no applied migration; refusing to proceed"
            )
