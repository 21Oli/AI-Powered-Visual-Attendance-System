"""Step 1.2 database-layer verification (Phase 3 — PostgreSQL backend).

Covers the lazy connection factory in ``src/db.py``:

1. Import hygiene: importing ``src.db`` must never import psycopg, the
   CV stack, or any attendance/journal module, and must attempt no
   connection (fresh-process subprocess proof, no side effects).
2. ``parse_dsn``: conninfo extraction for URI-style and keyword-style
   DSNs; blank-DSN rejection; transparent ValueError contract for
   psycopg-side syntax errors.
3. ``open_connection``: lazy resolution of the configured DSN, verbatim
   forwarding of an explicit dsn, typed ``DatabaseUnavailableError``
   wrapping for connection failures, transparent ValueError contract,
   and no connection unless explicitly invoked.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.config import ConfigError
from src.db import DatabaseUnavailableError, open_connection, parse_dsn


# ---------------------------------------------------------------------------
# 1. Import hygiene (fresh process; no connection, no heavyweight imports)
# ---------------------------------------------------------------------------


def test_db_import_is_lazy_and_pulls_no_stack() -> None:
    """A fresh ``import src.db`` must not load psycopg, any CV library,
    any attendance/journal module, or any web framework, and must not
    attempt a PostgreSQL connection (nothing imports the driver at all)."""
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.db;"
        "banned = {'psycopg', 'cv2', 'mediapipe', 'insightface', "
        "'onnxruntime', 'fastapi', 'uvicorn',"
        "'src.attendance', 'src.attendance_store', 'src.camera'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
        "assert not hasattr(src.db, 'psycopg'), 'module-level psycopg leak';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


# ---------------------------------------------------------------------------
# 2. parse_dsn (pure syntax; no network)
# ---------------------------------------------------------------------------


def test_parse_dsn_extracts_uri_components() -> None:
    info = parse_dsn(
        "postgresql://db_user:must-not-leak@127.0.0.1:5433/attendance_test"
    )
    assert info["host"] == "127.0.0.1"
    assert info["dbname"] == "attendance_test"
    assert int(info["port"]) == 5433
    # Password never materializes unredacted in the returned mapping.
    assert info.get("password") not in (None, "must-not-leak") or str(
        info.get("password")
    ).startswith("*")


def test_parse_dsn_accepts_keyword_style_dsn_identically() -> None:
    # URI and keyword conninfo are equally supported — no scheme filtering.
    info = parse_dsn("host=127.0.0.1 dbname=attendance_test port=5433")
    assert info["host"] == "127.0.0.1"
    assert info["dbname"] == "attendance_test"
    assert int(info["port"]) == 5433


@pytest.mark.parametrize("dsn", ["", "   ", "\t"])
def test_parse_dsn_rejects_blank_target(dsn: str) -> None:
    # A blank DSN is never a valid destination: availability failure.
    with pytest.raises(DatabaseUnavailableError):
        parse_dsn(dsn)


def test_parse_dsn_keeps_valueerror_failures_transparent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psycopg-side DSN *syntax* contract violations stay ValueError-shaped."""
    sentinel = ValueError("simulated psycopg conninfo syntax rejection")

    def raiser(*args: object, **kwargs: object) -> str:
        raise sentinel

    monkeypatch.setattr("psycopg.conninfo.make_conninfo", raiser)
    with pytest.raises(ValueError) as err:
        parse_dsn("postgresql://localhost/attendance")
    assert err.value is sentinel
    assert not isinstance(err.value, DatabaseUnavailableError)


def test_parse_dsn_masks_password_in_returned_mapping() -> None:
    info = parse_dsn("postgresql://u:secret@localhost:5432/attendance")
    password = info.get("password")
    assert password is not None
    assert "secret" not in str(password)


# ---------------------------------------------------------------------------
# 3. open_connection (lazy; factual failure wrapping)
# ---------------------------------------------------------------------------


def test_open_connection_forwards_explicit_dsn_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    sentinel_conn = object()

    def fake_connect(conninfo: str) -> object:
        calls.append(conninfo)
        return sentinel_conn

    monkeypatch.setattr("src.db._connect", fake_connect)
    dsn = "postgresql://u:p@localhost:5432/attendance"
    conn = open_connection(dsn)
    assert conn is sentinel_conn
    assert calls == [dsn]


def test_open_connection_resolves_configured_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ATTENDANCE_DATABASE_URL", "postgresql://u:p@localhost:5432/attendance"
    )
    calls: list[str] = []

    def fake_connect(conninfo: str) -> object:
        calls.append(conninfo)
        return object()

    monkeypatch.setattr("src.db._connect", fake_connect)
    open_connection()
    assert calls == ["postgresql://u:p@localhost:5432/attendance"]


def test_open_connection_blank_explicit_dsn_is_availability_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("ATTENDANCE_DATABASE_URL",):
        monkeypatch.setenv(var, "")  # real env beats .env; "" maps to unset
    with pytest.raises(DatabaseUnavailableError):
        open_connection()


def test_open_connection_unavailable_failure_is_typed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_connect(conninfo: str) -> object:
        raise ConnectionError("host unreachable")

    monkeypatch.setattr("src.db._connect", failing_connect)
    dsn = "postgresql://u:secret@localhost:5432/attendance"
    with pytest.raises(DatabaseUnavailableError) as info:
        open_connection(dsn)
    # No credential material in the message; the failure cause is preserved.
    assert "secret" not in str(info.value)
    assert "u:p" not in str(info.value)
    assert isinstance(info.value.__cause__, ConnectionError)


def test_open_connection_keeps_valueerror_failures_transparent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def contract_violating_connect(conninfo: str) -> object:
        raise ValueError("unknown conninfo option")

    monkeypatch.setattr("src.db._connect", contract_violating_connect)
    with pytest.raises(ValueError) as err:
        open_connection("postgresql://localhost/attendance")
    assert not isinstance(err.value, DatabaseUnavailableError)


def test_config_error_is_value_error_family() -> None:
    # ConfigError contract unchanged (asserted here too, because db.py
    # propagates ConfigError transparently per its documented contract).
    assert issubclass(ConfigError, ValueError)


# ---------------------------------------------------------------------------
# 4. Real-parser credential-leak regressions (Gate 1.2 audit finding 1)
#
# These use the INSTALLED psycopg parser — no monkeypatching of
# make_conninfo — because the audited leak was in the real driver's
# behavior: psycopg's own parse-error message echoes the whole DSN
# (password included) for malformed URIs. Mock only the connection
# indirection, to prove no connection is reached.
# ---------------------------------------------------------------------------


_REAL_MALFORMED_URI = "postgresql://user:audit-secret@[::1/db"


def test_parse_dsn_real_parser_malformed_uri_sanitized() -> None:
    """The real psycopg parser's malformed-URI failure must surface as a
    sanitized, ValueError-shaped MalformedDsnError: no password, no DSN,
    no hostname, and no raw driver error text in the message."""
    with pytest.raises(ValueError) as err:
        # Real psycopg.conninfo.make_conninfo runs here (no monkeypatching).
        parse_dsn(_REAL_MALFORMED_URI)
    message = str(err.value)
    assert "audit-secret" not in message, "password leaked in error message"
    assert _REAL_MALFORMED_URI not in message, "raw DSN echoed in error message"
    assert "user:audit-secret@[::1/db" not in message, "host leaked"
    assert "audit-secret" not in repr(err.value)
    # No raw driver text either: the only tolerated echo is the type name.
    assert "user:" not in message
    # Documented taxonomy: ValueError-shaped, availability-class separate.
    assert not isinstance(err.value, DatabaseUnavailableError)
    # Trusted diagnostics: real driver exception preserved as __cause__.
    assert err.value.__cause__ is not None


def test_parse_dsn_real_parser_malformed_uri_is_dedicated_type() -> None:
    """The sanitized error is the dedicated MalformedDsnError class."""
    from src.db import MalformedDsnError

    with pytest.raises(MalformedDsnError):
        parse_dsn(_REAL_MALFORMED_URI)


def test_open_connection_real_parser_malformed_uri_never_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same malformed URI through open_connection: sanitized ValueError,
    and the connection indirection is never invoked (failure happens at
    the parse boundary, before any database activity)."""
    calls: list[str] = []

    def forbidden_connect(conninfo: str) -> object:
        calls.append(conninfo)
        raise AssertionError("connect must not be reached for a malformed DSN")

    monkeypatch.setattr("src.db._connect", forbidden_connect)
    with pytest.raises(ValueError) as err:
        open_connection(_REAL_MALFORMED_URI)
    message = str(err.value)
    assert "audit-secret" not in message
    assert _REAL_MALFORMED_URI not in message
    assert calls == [], "a connection attempt occurred (or was reachable)"
    assert not isinstance(err.value, DatabaseUnavailableError)


def test_parse_dsn_masks_password_and_sslpassword_in_keyword_dsn() -> None:
    """Both literal-secret conninfo keys are masked key-aware in the
    returned mapping's reserialization (keyword-style DSN)."""
    dsn = (
        "host=127.0.0.1 port=5432 dbname=attendance user=u "
        "password=tls-secret sslpassword=cert-secret"
    )
    info = parse_dsn(dsn)
    rendered = str(info)
    assert "tls-secret" not in rendered, "password leaked in returned mapping"
    assert "cert-secret" not in rendered, "sslpassword leaked in mapping"
    assert info.get("password") == "***"
    assert info.get("sslpassword") == "***"
    # Other keys keep their real values (key-aware, not broad replacement).
    assert info["dbname"] == "attendance"
    assert info["user"] == "u"


def test_open_connection_unavailable_error_masks_sslpassword_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The availability error message embeds only the redacted conninfo:
    both secret keys must already be masked there."""

    def failing_connect(conninfo: str) -> object:
        raise ConnectionError("host unreachable")

    monkeypatch.setattr("src.db._connect", failing_connect)
    combined = (
        "host=127.0.0.1 port=5432 dbname=attendance user=u "
        "password=secret sslpassword=cert-secret"
    )
    with pytest.raises(DatabaseUnavailableError) as info:
        open_connection(combined)
    rendered = str(info.value)
    assert "cert-secret" not in rendered, "sslpassword leaked in error message"
    assert "sslpassword=***" in rendered, "sslpassword not acknowledged as masked"
    assert "password=secret" not in rendered
    assert "password=***" in rendered
    assert isinstance(info.value.__cause__, ConnectionError)
