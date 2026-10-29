"""Lazy PostgreSQL connection layer for the Phase 3 backend (Step 1.2).

Declarative surface only — no employee repository, migrations, or API
endpoints are present yet (they arrive in later Phase 3 gates). Later
steps import the lazy helpers from this module instead of importing
psycopg ad-hoc.

Design rules
------------
* **No side effects on import.** Importing this module opens no network
  connection, loads no native driver, touches no files, and reads no
  journals — in particular it imports **no attendance, camera, or
  pipeline modules** and does **not** import psycopg at module level
  (the driver is imported lazily, only on actual use). Allowed imports
  are only ``src.config``, which is itself import-safe without a CV
  stack (hygiene-tested).
* **Lazy connections.** Nothing connects at import time. A connection is
  only opened when :func:`open_connection` is explicitly invoked.
* **Typed errors at the boundary, two separable families.** Connection
  and availability problems surface as
  :class:`DatabaseUnavailableError` (never swallowed into a falsy
  value); contract violations (blank DSN, malformed DSN syntax) stay
  ``ValueError``-shaped — the repo-wide "validate inputs, fail fast,
  never silently coerce" rule. The taxonomy:

  - Caller-value contract mistakes (blank target, malformed DSN
    syntax) → ``ValueError``-shaped; never wrapped into an
    availability error.
  - Infrastructure/availability failures (cannot reach PostgreSQL) →
    :class:`DatabaseUnavailableError`, carrying the redacted DSN and
    the underlying exception *type name* only (never its full exception
    text, so no driver-internal secret can leak through the message).

* **No credential leakage.** DSN handling never logs, prints, or embeds
  a raw DSN anywhere in reachable error messages. Passwords are masked
  via psycopg's own redaction helper before any DSN fragment can be
  embedded; the original exception is preserved as ``__cause__`` for
  trusted (never endpoint-facing) diagnostics.
* **No implicit retry.** One failure = one raised error. The composition
  layer decides whether/when to retry — never the connection layer.
"""

from __future__ import annotations

import time

from src.config import ConfigError, load_settings

__all__ = [
    "DatabaseUnavailableError",
    "MalformedDsnError",
    "parse_dsn",
    "open_connection",
]


class DatabaseUnavailableError(RuntimeError):
    """Raised when a PostgreSQL connection cannot be established.

    Deliberately **not** a ``ValueError``: it means "infrastructure-side
    failure", not "the caller's value violated the contract".
    """


class MalformedDsnError(ValueError):
    """Raised when psycopg rejects a DSN's syntax (malformed URI/kv conninfo).

    ``ValueError``-shaped per the module taxonomy: it means "the caller's
    value violated the contract", not an infrastructure failure.

    The message is **sanitized**: it never contains the DSN, username,
    password, hostname, or any raw driver error text. Credentials can
    genuinely appear in psycopg's own parse-error messages (audited on
    psycopg 3.3.6: a malformed IPv6 URI is echoed verbatim into the
    ``ProgrammingError`` message), so this boundary replaces that text
    with a generic message plus the underlying exception *type name*
    only. The original exception is preserved as ``__cause__`` for
    trusted (never endpoint-facing) diagnostics.
    """


# ---------------------------------------------------------------------------
# psycopg access — always lazy, so importing src.db stays side-effect free
# ---------------------------------------------------------------------------


def _conninfo_module():
    """Return the ``psycopg.conninfo`` module, importing it on demand."""
    import psycopg.conninfo  # lazy: keeps the module import side-effect free

    return psycopg.conninfo


# Conninfo keys whose values are literal secrets; masked before any DSN
# fragment can be embedded in an error message or returned mapping.
_SECRET_KEYS: frozenset[str] = frozenset({"password", "sslpassword"})

# ``passfile`` is deliberately NOT masked: its value is a filesystem *path*
# (libpq's password file, usually ~/.pgpass), not a secret itself — the
# secrets live in the file's contents, which never pass through this layer.
# Masking the path would cost useful diagnostics for zero security gain.


def _redact(conninfo: str) -> str:
    """Return ``conninfo`` with every secret-valued key masked to ``***``.

    Built on psycopg's own public parsing primitives (this release has no
    dedicated redaction helper): the conninfo is parsed with
    ``conninfo_to_dict``, the secret keys (:data:`_SECRET_KEYS`) are
    overwritten, and the string is re-serialized with ``make_conninfo`` —
    all public API, no driver internals. Key-aware masking (no broad
    string replacement, so values like ``***`` inside unrelated keys are
    untouched).
    """
    info = dict(_conninfo_module().conninfo_to_dict(conninfo))
    for key in _SECRET_KEYS:
        if key in info:
            info[key] = "***"
    clean = {key: value for key, value in info.items() if value is not None}
    return _conninfo_module().make_conninfo("", **clean)


def _connect(conninfo: str):
    """Open a real psycopg connection (lazy driver import on use).

    Kept as a module-level indirection so tests can inject a fake
    connection function without any network activity.
    """
    import psycopg  # lazy: keeps the module import side-effect free

    return psycopg.connect(conninfo=conninfo)


def _make_conninfo(dsn: str) -> str:
    """Normalize a DSN (URI or keyword/value style) to psycopg conninfo.

    Raises
    ------
    ValueError
        If ``dsn``'s syntax violates psycopg's contract (bad quoting,
        unrecognized URI shape, ...). Constructor messages carry no
        exception internals beyond the type name.
    DatabaseUnavailableError
        If ``dsn`` is empty or whitespace-only (a blank DSN is never a
        valid destination; psycopg would otherwise silently connect to
        a socket/database default, which is forbidden here).
    """
    if dsn is None or not dsn.strip():
        # Forbid the psycopg "connect to whatever the local default is"
        # behavior silently: an unknown destination is an availability
        # failure at this layer, not a parseable value.
        raise DatabaseUnavailableError(
            "No usable PostgreSQL connection target (blank DSN). "
            "Configure ATTENDANCE_DATABASE_URL or pass an explicit dsn."
        )
    try:
        return _conninfo_module().make_conninfo(dsn)
    except ValueError:
        # Already ValueError-shaped (contract-conformant parser behavior;
        # the injected-parser unit tests rely on transparent pass-through).
        raise
    except Exception as exc:
        # The relevant psycopg parsing-error family (psycopg.Error
        # subclasses such as ProgrammingError — and, defensively, any
        # other non-ValueError failure, e.g. TypeError for non-str DSNs).
        # Psycopg's own messages *can* embed the caller's whole DSN,
        # credentials included (audited: a malformed IPv6 URI is echoed
        # verbatim). Sanitize to the documented ValueError-shaped
        # contract: no DSN, username, password, hostname, or raw driver
        # text survives into the message; the original exception is
        # chained as __cause__ for trusted diagnostics only.
        raise MalformedDsnError(
            "Malformed PostgreSQL DSN: psycopg rejected the connection "
            f"string syntax (underlying exception type: {type(exc).__name__})"
        ) from exc


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def parse_dsn(dsn: str) -> dict:
    """Parse a DSN into its conninfo parameters (diagnostics only).

    Pure syntax evaluation — **no network activity** ever.

    Parameters
    ----------
    dsn:
        A PostgreSQL DSN in URI style (``postgresql://...``) or
        keyword/value style (``host=... dbname=...``). The supported
        syntax is exactly psycopg's own; this module adds no dialect of
        its own.

    Returns
    -------
    dict
        The conninfo key/value mapping (psycopg's normalization). The
        ``password`` value, if present, is masked before the mapping is
        materialized, so the returned dict is safe to log.
        URI-style and keyword-style DSNs are accepted identically — no
        scheme filtering.

    Raises
    ------
    DatabaseUnavailableError
        If ``dsn`` is empty/whitespace-only (no parseable destination).
    MalformedDsnError
        If psycopg rejects the DSN syntax. ``MalformedDsnError`` is a
        ``ValueError`` subclass, so the ValueError-shaped contract holds.
        The message is sanitized (no DSN, credentials, host, or raw
        driver text); the underlying driver exception is chained as
        ``__cause__`` for trusted diagnostics only.
    ValueError
        Passes through transparently if raised directly as a plain
        ``ValueError`` (the pre-existing pass-through contract; real
        psycopg parse errors arrive as ``psycopg.Error`` family members
        and are sanitized above).
    """
    conninfo: str = _make_conninfo(dsn)
    return dict(_conninfo_module().conninfo_to_dict(_redact(conninfo)))


def open_connection(dsn: str | None = None):
    """Open a live PostgreSQL connection lazily, on demand.

    Resolution priority (documented): (1) an explicit ``dsn`` argument;
    (2) ``load_settings().database_url`` — the
    ``ATTENDANCE_DATABASE_URL`` configuration — re-read on *every* call,
    never cached, so callers and tests always observe the current
    configuration rather than a stale module-level snapshot. Connection
    setup is rare (once per connection), so the tiny re-read cost buys
    deterministic current-value semantics.

    Parameters
    ----------
    dsn:
        Explicit PostgreSQL DSN. Takes strict precedence over the
        configured value when provided.

    Returns
    -------
    A live psycopg connection (a real ``psycopg.Connection``). The
    caller is its owner and is responsible for closing it.

    Raises
    ------
    DatabaseUnavailableError
        When no destination is configured (and none was passed), or the
        connection cannot be established (availability-class failure;
        never silently swallowed, never auto-retried).
    ValueError
        For DSN *validity* errors (malformed conninfo/URI, raised as the
        ``ValueError`` subclass :class:`MalformedDsnError` with a
        sanitized message): the caller supplied an unusable connection
        target — a contract issue, not an availability issue.
    ConfigError
        Propagates transparently if the environment config layer
        (:func:`src.config.load_settings`) rejects the current
        environment — that is the config layer's own documented
        contract (:class:`~src.config.ConfigError`).
    """

    # Resolution: explicit argument beats the environment, resolved
    # fresh on every call.
    resolved = dsn if dsn is not None else load_settings().database_url

    if resolved is None or not resolved.strip():
        raise DatabaseUnavailableError(
            "No usable PostgreSQL connection target: " "ATTENDANCE_DATABASE_URL is unset (and no explicit dsn was given)."
        )

    try:
        conninfo: str = _make_conninfo(resolved)
    except ValueError:
        # Malformed DSN is the caller's value error: re-raise unchanged,
        # consistent with parse_dsn's contract.
        raise

    started = time.perf_counter()
    try:
        return _connect(conninfo)
    except ValueError:
        # psycopg-side conninfo contract error (e.g. an unknown option
        # value): still the caller's conninfo value — keep it
        # ValueError-shaped, consistent with the module's taxonomy.
        raise
    except Exception as exc:  # availability-class failure; typed at the edge
        elapsed = time.perf_counter() - started
        raise DatabaseUnavailableError(
            f"PostgreSQL connection failed after {elapsed:.6f}s "
            f"(redacted conninfo: {_redact(conninfo)}); "
            f"underlying exception type: {type(exc).__name__}"
        ) from exc
