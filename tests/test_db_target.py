"""Offline tests for :mod:`src.db_target` (Phase 3 — Step 1.5b-6).

No connection is opened: target normalization is pure string/DSN work.
"""

from __future__ import annotations

import pytest

from src.db import MalformedDsnError
from src.db_target import normalized_target_identity, same_target


def test_local_host_aliases_collapse_to_one_identity() -> None:
    first = normalized_target_identity("postgresql://alice:pw@localhost:5432/app")
    second = normalized_target_identity("postgresql://bob:other@127.0.0.1:5432/app")
    assert first == second == ("app", "_local_server_", 5432)


def test_credentials_are_not_part_of_the_identity() -> None:
    assert normalized_target_identity(
        "postgresql://a:secret@localhost/app"
    ) == normalized_target_identity("postgresql://b:different@localhost/app")


def test_missing_port_uses_the_libpq_default() -> None:
    assert normalized_target_identity("host=localhost dbname=app") == (
        "app",
        "_local_server_",
        5432,
    )


def test_explicit_port_is_kept_and_distinguishes_targets() -> None:
    default_port = normalized_target_identity("host=localhost dbname=app port=5432")
    other_port = normalized_target_identity("host=localhost dbname=app port=5433")
    assert default_port != other_port
    assert other_port == ("app", "_local_server_", 5433)


def test_different_databases_are_distinct() -> None:
    assert not same_target(
        "postgresql://u:p@localhost/app", "postgresql://u:p@localhost/other"
    )


def test_same_database_in_different_spellings_is_the_same_target() -> None:
    assert same_target(
        "postgresql://u:p@localhost:5432/app",
        "postgresql://u:p@127.0.0.1/app",
    )


@pytest.mark.parametrize(
    "dsn",
    [
        "host=localhost dbname=app hostaddr=10.0.0.1",
        "service=phase3 dbname=app",
        "dbname=app",  # no host: libpq local-socket default is not comparable
        "host=localhost",  # no dbname
        "host=localhost dbname=app port=not-a-port",
    ],
)
def test_ambiguous_targets_have_no_identity(dsn: str) -> None:
    assert normalized_target_identity(dsn) is None


@pytest.mark.parametrize("dsn", ["", "   ", None, 42])
def test_blank_or_non_string_dsn_has_no_identity(dsn: object) -> None:
    assert normalized_target_identity(dsn) is None  # type: ignore[arg-type]


def test_ambiguous_identity_is_never_the_same_target() -> None:
    assert same_target("dbname=app", "dbname=app") is False
    assert same_target("postgresql://u:p@localhost/app", "service=x dbname=app") is False


def test_malformed_dsn_raises_the_configuration_error() -> None:
    with pytest.raises(MalformedDsnError):
        normalized_target_identity("postgresql://u:p@[::1/app")
