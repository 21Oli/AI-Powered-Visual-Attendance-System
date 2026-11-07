"""Normalized PostgreSQL target identity (Phase 3 — Step 1.5b-6).

One job only: answer "do these two DSNs describe the same database?" without
opening a connection. Two callers need exactly this:

* ``tools/run_migrations.py`` — refuses a migration target that resolves to
  the dedicated Tier-2 test database, and fails closed when a configured test
  DSN cannot be normalized at all.
* the Tier-2 migration suite — binds an executor's DSN to the
  ``VerifiedTestTarget`` the guard returned, so a guard pass can never
  authorize a different connection target.

Relationship to the guard
-------------------------
:mod:`src.test_database_guard` remains the *authorization* authority: only its
positive, server-side evidence permits a database to be treated as the
dedicated test database. This module is a *routing* check with the same
normalization rules (host aliases collapsed, libpq's default port, rerouting
keys refused) so the two cannot disagree about what "the same target" means.
It is explicitly **not** a security boundary and it never replaces the guard.

Normalization rules (mirrored from the guard's documented contract)
------------------------------------------------------------------
* ``hostaddr`` / ``service`` reroute a connection away from the host named in
  the same DSN → identity is ambiguous (``None``).
* A missing/blank ``dbname`` or ``host`` → ambiguous (a missing host means the
  libpq local-socket default, which cannot be compared with a TCP host).
* A missing port is **not** ambiguous: libpq's default is fixed (5432).
* ``localhost`` / ``127.0.0.1`` / ``::1`` / ``host.docker.internal`` collapse
  to one local-server token, so two spellings of the same server compare
  equal (never unequal, which would be the dangerous direction).
* User and password are deliberately excluded: different credentials on the
  same database never make it a different database.

No driver import happens here; the DSN parser is :func:`src.db.parse_dsn`,
whose ``password`` value is masked before it is returned.
"""

from __future__ import annotations

from typing import Final

from src.db import parse_dsn

__all__ = ["normalized_target_identity", "same_target"]

#: libpq's documented default port (see the guard's ``_DEFAULT_PORT``).
_DEFAULT_PORT: Final[int] = 5432

#: Conninfo keys that reroute a connection away from the named host.
_REROUTING_KEYS: Final[frozenset[str]] = frozenset({"hostaddr", "service"})

#: Textual host variants naming this single-machine deployment's local server.
_LOCAL_HOST_ALIASES: Final[frozenset[str]] = frozenset(
    {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
)
_LOCAL_HOST_TOKEN: Final[str] = "_local_server_"


def normalized_target_identity(dsn: str) -> tuple[str, str, int] | None:
    """Return ``(dbname, host, port)`` for ``dsn``, or ``None`` if ambiguous.

    ``None`` means "this identity cannot be positively established" — the
    caller must treat that as a refusal (fail closed), never as permission.

    Raises
    ------
    MalformedDsnError
        When the DSN's syntax is rejected by the driver's parser. That is a
        caller-value contract failure, distinct from ambiguity, and is left
        for the caller to map to its own configuration-error path.
    """
    if not isinstance(dsn, str) or not dsn.strip():
        return None

    mapping = parse_dsn(dsn)

    if any(key in mapping for key in _REROUTING_KEYS):
        return None

    dbname = mapping.get("dbname")
    if not isinstance(dbname, str) or not dbname.strip():
        return None

    host_raw = mapping.get("host")
    if not isinstance(host_raw, str) or not host_raw.strip():
        return None
    host = host_raw.strip().lower()
    if host in _LOCAL_HOST_ALIASES:
        host = _LOCAL_HOST_TOKEN

    port_raw = mapping.get("port")
    if port_raw is None:
        port = _DEFAULT_PORT
    else:
        try:
            port = int(str(port_raw).strip())
        except (TypeError, ValueError):
            return None
        if not 1 <= port <= 65535:
            return None

    return (dbname.strip(), host, port)


def same_target(first_dsn: str, second_dsn: str) -> bool:
    """Whether two DSNs describe the same database identity.

    Ambiguous identities are never "the same": this returns ``False`` when
    either side cannot be established. Callers that must fail closed (the
    migration CLI) test for ``None`` explicitly rather than relying on this.
    """
    first = normalized_target_identity(first_dsn)
    second = normalized_target_identity(second_dsn)
    if first is None or second is None:
        return False
    return first == second
