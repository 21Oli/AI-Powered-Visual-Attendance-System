"""CLI tests for first-Admin provisioning (P5.6).

Offline only: the repositories, settings and audit subsystem are replaced by
in-memory doubles, so no database, DSN or real credential is involved. The
tests pin the operator-facing contract: a successful run prints identity only,
a refusal exits non-zero with a sanitized message, and the password never
appears in output.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from src import user_provision
from src.user import Role, User
from src.user_repository import LastAdminProtectionError, UserAlreadyExists
from src.user_service import UserAdministrationError

NOW = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
PASSWORD = "correct-horse-battery"


class _FakeIntent:
    def __init__(self, identifier: str) -> None:
        self.identifier = identifier


class _FakeAudit:
    """Records the intent/result protocol calls the service makes."""

    def __init__(self, fail_begin: bool = False) -> None:
        self.begins: list[str] = []
        self.results: list[str] = []
        self.fail_begin = fail_begin

    def begin_first_admin_provision(self, *, target_id, context, source=None):
        if self.fail_begin:
            from src.audit_service import AuditUnavailableError

            raise AuditUnavailableError("closed")
        self.begins.append(target_id)
        return _FakeIntent(target_id)

    def record_applied(self, intent, *, metadata=None):
        self.results.append("success")
        return _FakeIntent(intent.identifier)

    def record_not_applied(self, intent, reason_code, *, metadata=None):
        self.results.append(f"error:{reason_code}")
        return _FakeIntent(intent.identifier)

    def record_unknown(self, intent, *, reason_code=None, metadata=None):
        self.results.append("uncertain")
        return _FakeIntent(intent.identifier)


class _FakeUsers:
    def __init__(self, existing: list[User] | None = None) -> None:
        self.rows = {u.user_id: u for u in (existing or [])}

    def provision_first_admin(self, user: User, *, updated_at: datetime) -> User:
        if any(u.role is Role.ADMIN and u.is_active for u in self.rows.values()):
            raise LastAdminProtectionError("an active administrator already exists")
        if user.user_id in self.rows:
            raise UserAlreadyExists("duplicate user_id")
        if any(
            row.username_key == user.username_key
            for row in self.rows.values()
        ):
            raise UserAlreadyExists("duplicate username_key")
        self.rows[user.user_id] = user
        return user


class _FakeSubsystem:
    def __init__(self, service) -> None:
        self._service = service

    def service(self):
        return self._service


def _wire(monkeypatch, audit, users):
    class _Settings:
        database_url = "postgresql://unused"

    monkeypatch.setattr(user_provision, "load_settings", lambda: _Settings())
    monkeypatch.setattr(user_provision, "open_user_repository", lambda **kw: users)
    monkeypatch.setattr(user_provision, "open_session_repository", lambda **kw: object())
    monkeypatch.setattr(
        user_provision, "AuditSubsystem", lambda builder=None: _FakeSubsystem(audit)
    )
    monkeypatch.setattr(user_provision, "_confirm_password", lambda prompt="": PASSWORD)


def test_module_is_importable_and_has_a_cli(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        user_provision.main(["--help"])
    assert exc.value.code == 0
    assert "provision-first-admin" in capsys.readouterr().out


def test_successful_provisioning_prints_identity_only(monkeypatch, capsys):
    audit = _FakeAudit()
    users = _FakeUsers()
    _wire(monkeypatch, audit, users)

    result = user_provision.provision_first_admin(
        user_id="root", username="rootadmin"
    )

    assert result.user_id == "root"
    assert result.role is Role.ADMIN
    assert users.rows["root"].role is Role.ADMIN
    assert audit.begins == ["root"]
    assert audit.results == ["success"]

    out = capsys.readouterr()
    assert "root" in out.out
    assert PASSWORD not in out.out
    assert PASSWORD not in out.err
    assert "$argon2id$" not in out.out


def test_refusal_when_an_active_admin_exists(monkeypatch, capsys):
    audit = _FakeAudit()
    existing = User(
        user_id="admin",
        username="existing-admin",
        password_hash="$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA",
        role=Role.ADMIN,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )
    _wire(monkeypatch, audit, _FakeUsers([existing]))

    with pytest.raises(SystemExit) as exc:
        user_provision.provision_first_admin(user_id="root", username="rootadmin")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert PASSWORD not in err
    assert len(audit.results) == 1
    assert audit.results[0].startswith("error:")
    assert audit.results[0].endswith("VALIDATION_FAILED")


def test_audit_unavailable_aborts_without_creating_a_user(monkeypatch, capsys):
    audit = _FakeAudit(fail_begin=True)
    users = _FakeUsers()
    _wire(monkeypatch, audit, users)

    with pytest.raises(SystemExit) as exc:
        user_provision.provision_first_admin(user_id="root", username="rootadmin")

    assert exc.value.code == 1
    assert "Audit subsystem unavailable" in capsys.readouterr().err
    assert users.rows == {}


def test_invalid_account_details_are_sanitized(monkeypatch, capsys):
    audit = _FakeAudit()
    _wire(monkeypatch, audit, _FakeUsers())

    with pytest.raises(SystemExit) as exc:
        user_provision.provision_first_admin(user_id=" ", username="rootadmin")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Invalid account details" in err or "rejected" in err
    assert PASSWORD not in err


def test_duplicate_identity_is_translated_to_a_safe_error(monkeypatch, capsys):
    audit = _FakeAudit()
    existing = User(
        user_id="operator-1",
        username="RootAdmin",
        password_hash="$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA",
        role=Role.OPERATOR,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )
    _wire(monkeypatch, audit, _FakeUsers([existing]))

    with pytest.raises(SystemExit) as exc:
        user_provision.provision_first_admin(user_id="root", username="rootadmin")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "duplicate" not in err.lower()
    assert PASSWORD not in err
    assert "$argon2id$" not in err
    assert audit.results and audit.results[0].endswith("EMPLOYEE_EXISTS")


def test_no_public_http_surface_for_provisioning():
    """Provisioning must stay operator-only: no API route creates an Admin."""
    from src.api import APPROVED_ROUTES

    assert not any("provision" in path for path, _method in APPROVED_ROUTES)
