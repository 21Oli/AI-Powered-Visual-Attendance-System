"""G2 — startup database schema compatibility gate.

The API may only serve when the database's migration history **exactly**
matches the migration files shipped with this application **and** the physical
schema satisfies the declarative contract (G8). This module is that gate: it
resolves the two configured targets safely, runs the existing read-only
snapshot under the existing advisory lock on a single connection, and turns the
existing migration-state rules, the physical-schema evaluation and any failure
to obtain either into one typed, sanitized refusal.

What this module never does
---------------------------
* It never runs a migration: ``migrate()``, ``ensure_ledger()`` and every
  apply/repair operation are untouched. A missing ledger is refused, never
  created.
* It never writes: the verification transaction is ``REPEATABLE READ READ
  ONLY``, and the only statements issued are the read-only snapshot queries
  plus the nonblocking advisory-lock pair.
* **Physical compatibility (G8) is enforced here, but not defined here.**
  This module contains no catalog query, schema expectation or finding code:
  it asks the combined executor operation (S4) for one
  locked verification, handing it the S2 catalog probe
  (:func:`src.db_schema_catalog.read_public_catalog`) and the S3 evaluator
  (:func:`src.physical_schema.evaluate_physical_schema`), then refuses unless
  the returned evaluation is ``COMPATIBLE``. The probe runs on G2's own
  connection inside G2's own read-only transaction, after the migration
  history has been accepted, so there is no second connection, lock or
  transaction and a history refusal can never be replaced by a physical one.
* It never connects to the application database at import time. Nothing here
  runs until :func:`verify_startup_schema` is called from
  :func:`src.api.run_server`.

Target discipline (approved G2 policy)
--------------------------------------
Both ``ATTENDANCE_DATABASE_URL`` and ``ATTENDANCE_TEST_DATABASE_URL`` are
mandatory. The application DSN is the only connection target — the test DSN is
compared **only**, through the existing
:func:`src.db_target.normalized_target_identity`, and is never handed to a
connection opener. Missing/blank, malformed, ambiguous, or identical targets
all refuse startup.

Diagnostics
-----------
Every refusal is a :class:`StartupSchemaError` carrying a stable
:data:`DIAGNOSTIC_CODES` member and a **fixed** sanitized message from
:data:`_MESSAGES`. Messages never contain a DSN, username, password, SQL text,
driver text, filesystem path, or traceback; trusted detail stays on
``__cause__`` for server-side diagnosis. ``tools/run_api.py`` prints one line
to stderr and exits ``4``.

G8 refusals keep that shape: ``SCHEMA_INCOMPATIBLE`` (the physical schema is
definitely wrong), ``SCHEMA_INDETERMINATE`` (it cannot be confirmed — e.g. a
partitioned contracted table, which stays indeterminate by owner decision),
``SCHEMA_CATALOG_UNREADABLE`` (the catalog could not be read completely) and
the pre-existing ``SCHEMA_VERIFICATION_FAILED`` for an unexpected result or an
evaluator failure. The S3 findings travel with the error
(``StartupSchemaError.findings``) with their own stable codes intact, and are
safe to print: they carry identifiers and contract type spellings only, never
a default expression, SQL text, DSN, credential or driver message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Final

from src.config import AppSettings
from src.db import DatabaseUnavailableError, MalformedDsnError
from src.db_migrate import (
    MigrationChecksumError,
    MigrationCleanupError,
    MigrationContentionError,
    MigrationError,
    MigrationFileError,
    MigrationGapError,
    MigrationIntegrityError,
    MigrationMissingScriptError,
    MigrationSnapshot,
    MigrationState,
    load_migrations,
    read_state,
)
from src.db_migrate_executor import (
    LockedSchemaVerification,
    MigrationDriverError,
    MigrationTargetError,
    PsycopgMigrationExecutor,
)
from src.db_schema_catalog import CatalogProbeError, read_public_catalog
from src.db_target import normalized_target_identity
from src.physical_schema import (
    CompatibilityStatus,
    Finding,
    SchemaEvaluation,
    evaluate_physical_schema,
)

__all__ = [
    "DATABASE_CONFIGURATION_INVALID",
    "DATABASE_TARGET_INVALID",
    "DATABASE_UNAVAILABLE",
    "DIAGNOSTIC_CODES",
    "MIGRATIONS_PENDING",
    "MIGRATION_CHECKSUM_MISMATCH",
    "MIGRATION_HISTORY_INVALID",
    "MIGRATION_LEDGER_MISSING",
    "MIGRATION_LOCK_RELEASE_FAILED",
    "MIGRATION_LOCK_UNAVAILABLE",
    "MIGRATION_SET_EMPTY",
    "SCHEMA_CATALOG_UNREADABLE",
    "SCHEMA_INCOMPATIBLE",
    "SCHEMA_INDETERMINATE",
    "SCHEMA_VERIFICATION_FAILED",
    "TEST_DATABASE_TARGET_REJECTED",
    "UNKNOWN_MIGRATION",
    "StartupSchemaError",
    "evaluate_migration_state",
    "resolve_startup_targets",
    "verify_startup_schema",
]

# ---------------------------------------------------------------------------
# Diagnostic vocabulary (approved G2 list; stable machine surface)
# ---------------------------------------------------------------------------

DATABASE_CONFIGURATION_INVALID: Final[str] = "DATABASE_CONFIGURATION_INVALID"
DATABASE_TARGET_INVALID: Final[str] = "DATABASE_TARGET_INVALID"
TEST_DATABASE_TARGET_REJECTED: Final[str] = "TEST_DATABASE_TARGET_REJECTED"
DATABASE_UNAVAILABLE: Final[str] = "DATABASE_UNAVAILABLE"
MIGRATION_LOCK_UNAVAILABLE: Final[str] = "MIGRATION_LOCK_UNAVAILABLE"
MIGRATION_LOCK_RELEASE_FAILED: Final[str] = "MIGRATION_LOCK_RELEASE_FAILED"
MIGRATION_SET_EMPTY: Final[str] = "MIGRATION_SET_EMPTY"
MIGRATION_LEDGER_MISSING: Final[str] = "MIGRATION_LEDGER_MISSING"
MIGRATIONS_PENDING: Final[str] = "MIGRATIONS_PENDING"
MIGRATION_CHECKSUM_MISMATCH: Final[str] = "MIGRATION_CHECKSUM_MISMATCH"
UNKNOWN_MIGRATION: Final[str] = "UNKNOWN_MIGRATION"
MIGRATION_HISTORY_INVALID: Final[str] = "MIGRATION_HISTORY_INVALID"
SCHEMA_VERIFICATION_FAILED: Final[str] = "SCHEMA_VERIFICATION_FAILED"
#: G8: the physical schema is present but definitely does not satisfy the
#: contracted shape (S3 ``INCOMPATIBLE``).
SCHEMA_INCOMPATIBLE: Final[str] = "SCHEMA_INCOMPATIBLE"
#: G8: nothing is definitely wrong, but the physical schema cannot be confirmed
#: (S3 ``INDETERMINATE`` — e.g. a partitioned contracted table).
SCHEMA_INDETERMINATE: Final[str] = "SCHEMA_INDETERMINATE"
#: G8: the physical catalog could not be read completely (S2's
#: ``CatalogProbeError``, surfaced through S4 as a ``MigrationDriverError``).
SCHEMA_CATALOG_UNREADABLE: Final[str] = "SCHEMA_CATALOG_UNREADABLE"

#: Every code this gate can emit (documentation/test surface).
DIAGNOSTIC_CODES: Final[tuple[str, ...]] = (
    DATABASE_CONFIGURATION_INVALID,
    DATABASE_TARGET_INVALID,
    TEST_DATABASE_TARGET_REJECTED,
    DATABASE_UNAVAILABLE,
    MIGRATION_LOCK_UNAVAILABLE,
    MIGRATION_LOCK_RELEASE_FAILED,
    MIGRATION_SET_EMPTY,
    MIGRATION_LEDGER_MISSING,
    MIGRATIONS_PENDING,
    MIGRATION_CHECKSUM_MISMATCH,
    UNKNOWN_MIGRATION,
    MIGRATION_HISTORY_INVALID,
    SCHEMA_VERIFICATION_FAILED,
    SCHEMA_INCOMPATIBLE,
    SCHEMA_INDETERMINATE,
    SCHEMA_CATALOG_UNREADABLE,
)

#: Fixed sanitized message per code — never built from values.
_MESSAGES: Final[dict[str, str]] = {
    DATABASE_CONFIGURATION_INVALID: (
        "ATTENDANCE_DATABASE_URL and ATTENDANCE_TEST_DATABASE_URL must both "
        "be set and non-empty to start the API"
    ),
    DATABASE_TARGET_INVALID: (
        "the configured database target cannot be safely identified for "
        "schema verification"
    ),
    TEST_DATABASE_TARGET_REJECTED: (
        "the application and test database targets resolve to the same "
        "database; refusing to start the API"
    ),
    DATABASE_UNAVAILABLE: (
        "the application database could not be reached for schema verification"
    ),
    MIGRATION_LOCK_UNAVAILABLE: (
        "another migration run holds the migration advisory lock; refusing to "
        "start the API"
    ),
    MIGRATION_LOCK_RELEASE_FAILED: (
        "the schema-verification advisory lock release could not be confirmed; "
        "refusing to serve"
    ),
    MIGRATION_SET_EMPTY: (
        "no migration files are shipped with this application; refusing to "
        "start the API"
    ),
    MIGRATION_LEDGER_MISSING: (
        "the application database has no migration ledger; apply the shipped "
        "migrations before starting the API"
    ),
    MIGRATIONS_PENDING: (
        "the application database has migrations that are not applied; apply "
        "the shipped migrations before starting the API"
    ),
    MIGRATION_CHECKSUM_MISMATCH: (
        "an applied migration's shipped file contents changed since it was "
        "applied; refusing to start the API"
    ),
    UNKNOWN_MIGRATION: (
        "the migration ledger records a version that no shipped migration "
        "provides; refusing to start the API"
    ),
    MIGRATION_HISTORY_INVALID: (
        "the migration history is invalid (duplicate, non-contiguous, "
        "malformed, or not beginning at V001); refusing to start the API"
    ),
    SCHEMA_VERIFICATION_FAILED: (
        "the application database schema could not be verified; refusing to "
        "start the API"
    ),
    SCHEMA_INCOMPATIBLE: (
        "the application database's physical schema does not match the "
        "required schema; refusing to start the API"
    ),
    SCHEMA_INDETERMINATE: (
        "the application database's physical schema could not be confirmed as "
        "compatible; refusing to start the API"
    ),
    SCHEMA_CATALOG_UNREADABLE: (
        "the application database's physical schema could not be read "
        "completely; refusing to start the API"
    ),
}


class StartupSchemaError(RuntimeError):
    """G2/G8 refusal: a stable diagnostic ``code`` plus a fixed safe message.

    ``str(error)`` is ``"<CODE>: <message>"`` so a single stderr line names the
    documented code and the operator-facing explanation. The underlying failure
    (if any) is preserved as ``__cause__`` for trusted, server-side diagnostics
    only.

    ``findings`` carries the untouched S3 findings for a physical-schema
    refusal (empty for every G2 refusal). Each finding keeps its own stable
    ``SCHEMA_*`` code and safe identifiers, so an operator can see *which*
    objects failed without re-running anything.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        findings: tuple[Finding, ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.findings = findings
        super().__init__(f"{code}: {message}")

    @property
    def finding_codes(self) -> tuple[str, ...]:
        """The S3 codes of ``findings``, in evaluation order."""
        return tuple(finding.code for finding in self.findings)


def _refusal(code: str) -> StartupSchemaError:
    """Build the typed refusal for ``code`` (message is a fixed constant)."""
    return StartupSchemaError(code, _MESSAGES[code])


def _physical_refusal(evaluation: SchemaEvaluation) -> StartupSchemaError | None:
    """Turn an S3 evaluation into the documented refusal, or ``None``.

    ``COMPATIBLE`` is the only outcome that permits startup. A definite
    mismatch and an unconfirmable schema are *different* refusals (so an
    operator can tell drift from incompleteness), and both carry the untouched
    S3 findings. Any other status is a verification failure — never a pass,
    and never silently treated as either known outcome.
    """
    if evaluation.compatible:
        return None
    if evaluation.status is CompatibilityStatus.INCOMPATIBLE:
        code = SCHEMA_INCOMPATIBLE
    elif evaluation.status is CompatibilityStatus.INDETERMINATE:
        code = SCHEMA_INDETERMINATE
    else:
        code = SCHEMA_VERIFICATION_FAILED
    return StartupSchemaError(
        code, _MESSAGES[code], findings=tuple(evaluation.findings)
    )


def _enforce_physical_schema(verification: object) -> None:
    """Require the combined S4 result to exist and be ``COMPATIBLE``.

    A missing or unexpected result shape is refused as a verification failure:
    "no evaluation came back" must never be read as compatibility.
    """
    if not isinstance(verification, LockedSchemaVerification):
        raise _refusal(SCHEMA_VERIFICATION_FAILED)
    if not isinstance(verification.physical, SchemaEvaluation):
        raise _refusal(SCHEMA_VERIFICATION_FAILED)
    refusal = _physical_refusal(verification.physical)
    if refusal is not None:
        raise refusal


# ---------------------------------------------------------------------------
# Target safety
# ---------------------------------------------------------------------------


def _configured_dsn(value: object) -> str:
    """Return a usable configured DSN or refuse (missing/blank is a refusal)."""
    if not isinstance(value, str) or not value.strip():
        raise _refusal(DATABASE_CONFIGURATION_INVALID)
    return value.strip()


def _identity(dsn: str) -> tuple[str, str, int]:
    """Normalized identity of ``dsn``, or a refusal when it cannot be named."""
    try:
        identity = normalized_target_identity(dsn)
    except ValueError as exc:
        # MalformedDsnError (a ValueError) is sanitized at the src.db boundary:
        # its message carries no DSN, credentials, or driver text.
        raise _refusal(DATABASE_TARGET_INVALID) from exc
    if identity is None:
        raise _refusal(DATABASE_TARGET_INVALID)
    return identity


def resolve_startup_targets(settings: AppSettings) -> str:
    """Validate both configured targets and return the application DSN.

    Both DSNs must be present and non-blank, both identities must be
    positively establishable, and the two must not name the same database
    (normalization is :func:`src.db_target.normalized_target_identity`, so
    ``localhost``/``127.0.0.1``/``::1`` spellings are the same server).

    Returns the **application** DSN: the only value a connection opener ever
    receives. The test DSN is used for the comparison and nothing else.
    """
    application_dsn = _configured_dsn(settings.database_url)
    test_dsn = _configured_dsn(settings.test_database_url)

    application_identity = _identity(application_dsn)
    test_identity = _identity(test_dsn)

    if application_identity == test_identity:
        raise _refusal(TEST_DATABASE_TARGET_REJECTED)
    return application_dsn


# ---------------------------------------------------------------------------
# Migration-state policy
# ---------------------------------------------------------------------------


def _loaded_scripts(migrations_dir: Path | None):
    """Load and validate the shipped migration files (filesystem only)."""
    try:
        return load_migrations(migrations_dir)
    except MigrationFileError as exc:
        raise _refusal(MIGRATION_HISTORY_INVALID) from exc


def _derived_state(
    snapshot: MigrationSnapshot, migrations_dir: Path | None
) -> MigrationState:
    """Run the existing reconciliation, mapping each rule to its code."""
    try:
        return read_state(snapshot, migrations_dir)
    except MigrationChecksumError as exc:
        raise _refusal(MIGRATION_CHECKSUM_MISMATCH) from exc
    except MigrationMissingScriptError as exc:
        raise _refusal(UNKNOWN_MIGRATION) from exc
    except (MigrationIntegrityError, MigrationGapError) as exc:
        raise _refusal(MIGRATION_HISTORY_INVALID) from exc
    except MigrationFileError as exc:
        raise _refusal(MIGRATION_HISTORY_INVALID) from exc
    except MigrationError as exc:
        raise _refusal(SCHEMA_VERIFICATION_FAILED) from exc
    except Exception as exc:
        # An unexpected failure inside the existing reconciliation is still a
        # verification failure, never a raw exception on the startup path.
        raise _refusal(SCHEMA_VERIFICATION_FAILED) from exc


def evaluate_migration_state(
    snapshot: MigrationSnapshot,
    migrations_dir: Path | None = None,
) -> MigrationState:
    """Decide whether ``snapshot`` permits startup; raise a refusal otherwise.

    The rules are additive to the existing ones (``read_state`` still owns
    checksum/reconciliation/gap semantics); this function adds only what G2
    requires of an exactly-up-to-date database:

    1. the shipped migration set must not be empty → ``MIGRATION_SET_EMPTY``
       (the loader deliberately treats an absent/empty directory as valid);
    2. the ledger must exist → ``MIGRATION_LEDGER_MISSING`` (a missing ledger
       is not an error to ``read_state``);
    3. the shipped set must begin at V001 → ``MIGRATION_HISTORY_INVALID``;
    4. nothing may be pending → ``MIGRATIONS_PENDING``.

    Every other refusal comes from the existing reconciliation rules: checksum
    drift, duplicate rows, non-contiguous history, applied-but-missing scripts,
    malformed files, and ``employee`` present without a ledger. Physical schema
    drift is not inspected.
    """
    scripts = _loaded_scripts(migrations_dir)
    if not scripts:
        raise _refusal(MIGRATION_SET_EMPTY)

    state = _derived_state(snapshot, migrations_dir)

    if not state.ledger_present:
        raise _refusal(MIGRATION_LEDGER_MISSING)
    if scripts[0].version != 1:
        raise _refusal(MIGRATION_HISTORY_INVALID)
    if state.pending:
        raise _refusal(MIGRATIONS_PENDING)
    return state


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _default_executor_factory(dsn: str) -> PsycopgMigrationExecutor:
    """Build the real executor (the driver is imported only on connection)."""
    return PsycopgMigrationExecutor(dsn)


def verify_startup_schema(
    settings: AppSettings,
    *,
    migrations_dir: Path | None = None,
    executor_factory: Callable[[str], PsycopgMigrationExecutor] | None = None,
) -> None:
    """Run the whole G2+G8 gate; return ``None`` only when startup may proceed.

    Order: target validation (no connection) → one connection carrying lock,
    the read-only snapshot, the migration-history evaluation, the physical
    catalog read, the physical evaluation and the confirmed release. The
    executor's failures are translated here into the approved diagnostic
    codes:

    * availability → ``DATABASE_UNAVAILABLE``;
    * malformed/unnameable target → ``DATABASE_TARGET_INVALID``;
    * lock not acquired → ``MIGRATION_LOCK_UNAVAILABLE``;
    * release/close not confirmed → ``MIGRATION_LOCK_RELEASE_FAILED``;
    * malformed ledger → ``MIGRATION_HISTORY_INVALID``;
    * catalog unreadable → ``SCHEMA_CATALOG_UNREADABLE``;
    * physical schema definitely wrong → ``SCHEMA_INCOMPATIBLE``;
    * physical schema unconfirmed → ``SCHEMA_INDETERMINATE``;
    * anything else (including an evaluator failure) →
      ``SCHEMA_VERIFICATION_FAILED``.

    A G2 migration-history refusal keeps strict precedence: it is raised inside
    the locked operation before the catalog probe runs, so a physical
    conclusion can never replace a history refusal. ``COMPATIBLE`` is the only
    physical outcome that returns ``None``.

    ``executor_factory`` is the offline-test seam (a fake opener is injected
    through :class:`PsycopgMigrationExecutor`); production never passes it.
    """
    application_dsn = resolve_startup_targets(settings)
    make_executor = (
        _default_executor_factory if executor_factory is None else executor_factory
    )

    def evaluate(snapshot: MigrationSnapshot) -> None:
        evaluate_migration_state(snapshot, migrations_dir)

    try:
        # Construction is inside the protected path as well: an unexpected
        # factory/constructor failure must not escape as a raw traceback.
        executor = make_executor(application_dsn)
        verification = executor.verify_schema_locked(
            evaluate,
            catalog_probe=read_public_catalog,
            physical_evaluator=evaluate_physical_schema,
        )
        _enforce_physical_schema(verification)
    except StartupSchemaError:
        raise  # a policy refusal from evaluate() is already final
    except DatabaseUnavailableError as exc:
        raise _refusal(DATABASE_UNAVAILABLE) from exc
    except MalformedDsnError as exc:
        raise _refusal(DATABASE_TARGET_INVALID) from exc
    except MigrationTargetError as exc:
        raise _refusal(DATABASE_TARGET_INVALID) from exc
    except MigrationIntegrityError as exc:
        raise _refusal(MIGRATION_HISTORY_INVALID) from exc
    except MigrationChecksumError as exc:
        raise _refusal(MIGRATION_CHECKSUM_MISMATCH) from exc
    except MigrationMissingScriptError as exc:
        raise _refusal(UNKNOWN_MIGRATION) from exc
    except MigrationGapError as exc:
        raise _refusal(MIGRATION_HISTORY_INVALID) from exc
    except MigrationContentionError as exc:
        raise _refusal(MIGRATION_LOCK_UNAVAILABLE) from exc
    except MigrationCleanupError as exc:
        raise _refusal(MIGRATION_LOCK_RELEASE_FAILED) from exc
    except MigrationDriverError as exc:
        # S4 wraps S2's typed probe error as the cause of its driver error, so
        # a catalog-read failure keeps its own documented refusal instead of
        # being folded into a generic verification failure — and it is never
        # read as an empty or partial snapshot.
        if isinstance(exc.__cause__, CatalogProbeError):
            raise _refusal(SCHEMA_CATALOG_UNREADABLE) from exc
        raise _refusal(SCHEMA_VERIFICATION_FAILED) from exc
    except MigrationError as exc:
        # Any remaining migration failure is not a policy decision this gate
        # knows how to name: refuse as an unexpected verification failure.
        raise _refusal(SCHEMA_VERIFICATION_FAILED) from exc
    except Exception as exc:
        raise _refusal(SCHEMA_VERIFICATION_FAILED) from exc
