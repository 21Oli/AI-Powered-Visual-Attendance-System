"""First-Admin provisioning CLI (P5.6).

A single, operator-controlled, local-only command that creates the first Admin
account when no active Admin exists.

Design
-------
* **No network endpoint.** This is a CLI command only — there is no HTTP route
  that creates the first Admin.
* **No default credentials.** The operator supplies the username and password
  interactively; nothing is hardcoded.
* **No plaintext in process listings.** Credentials are read via
  ``getpass.getpass``, never as command-line arguments.
* **No logging of passwords.** The password is hashed immediately and never
  written to logs, stdout, or stderr.
* **Last-Admin safety.** Provisioning refuses if any active Admin already
  exists. The repository runs the "no active Admin" check and the insert in one
  explicitly ``READ COMMITTED`` transaction guarded by the shared admin-invariant
  advisory lock, so concurrent operators serialize and the loser exits with a
  sanitized error. The ``username_key`` UNIQUE constraint and the ``user_id``
  primary key remain the final authority for duplicate identity.
* **Audited.** Every provisioning attempt is recorded through the P5.5 audit
  service using the intent/result protocol, with ``source=cli``.
* **Safe transaction.** The password is hashed before the database write; the
  write is a single ``INSERT`` that either succeeds or raises a typed error.

Usage
-----
.. code-block:: bash

    python -m src.user_provision --help

or, from the project root:

.. code-block:: bash

    .venv/Scripts/python.exe -m src.user_provision

Exit codes
-----------
* ``0`` — provisioning succeeded.
* ``1`` — provisioning failed (already provisioned, validation error, database
  unavailable, audit unavailable, or other operational error).
* ``2`` — usage error (bad arguments).
"""

from __future__ import annotations

import argparse
import getpass
import sys

from src.api_audit import AuditSubsystem, production_audit_builder
from src.audit_service import AuditUnavailableError
from src.config import load_settings
from src.db import DatabaseUnavailableError
from src.db_security_repository import open_session_repository, open_user_repository
from src.password import PasswordService
from src.session_service import SessionService
from src.user import UserValidationError
from src.user_service import (
    AdminAlreadyExistsError,
    CreatedUser,
    PasswordTooShortError,
    UserAdministrationError,
    UserAlreadyExistsError,
    UserService,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="provision-first-admin",
        description="Create the first Admin account (operator-controlled, local only).",
    )
    parser.add_argument(
        "--user-id",
        type=str,
        default=None,
        help="Admin account identifier (default: prompted).",
    )
    parser.add_argument(
        "--username",
        type=str,
        default=None,
        help="Admin display name (default: prompted).",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="PostgreSQL DSN (default: ATTENDANCE_DATABASE_URL).",
    )
    return parser.parse_args(argv)


def _prompt_nonzero(prompt: str) -> str:
    """Read a non-empty line from the terminal, reprompting on blank input."""
    while True:
        value = input(prompt)
        if value.strip():
            return value
        print("This value cannot be empty.", file=sys.stderr)


def _prompt_password(prompt: str = "Password: ") -> str:
    """Read a password via getpass (not visible in process listings)."""
    while True:
        password = getpass.getpass(prompt)
        if password.strip():
            return password
        print("This value cannot be empty.", file=sys.stderr)


def _confirm_password(prompt: str = "Password: ") -> str:
    """Read and confirm a password."""
    password = _prompt_password(prompt)
    confirmation = _prompt_password("Confirm password again: ")
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return _confirm_password(prompt)
    return password


def provision_first_admin(
    *,
    user_id: str | None = None,
    username: str | None = None,
    database_url: str | None = None,
) -> CreatedUser:
    """Create the first Admin account.

    Parameters
    ----------
    user_id:
        Optional account identifier. When omitted, the operator is prompted.
    username:
        Optional display name. When omitted, the operator is prompted.
    database_url:
        Optional DSN override. When omitted, the environment setting is used.

    Returns
    -------
    CreatedUser
        The public result (never a password hash).

    Raises
    ------
    SystemExit
        On operational failure (already provisioned, validation error,
        database unavailable, etc.).
    """
    # Load settings (fails fast on invalid configuration, never reads secrets
    # into memory beyond what the DB layer needs).
    settings = load_settings()
    dsn = database_url or settings.database_url

    # Open repositories (lazy; fails closed on missing DSN).
    try:
        user_repository = open_user_repository(dsn=dsn)
        session_repository = open_session_repository(dsn=dsn)
    except DatabaseUnavailableError as exc:
        print("ERROR: Database is unavailable. Cannot provision.", file=sys.stderr)
        raise SystemExit(1) from exc

    # Password service (Argon2id hashing).
    password_service = PasswordService()

    # Audit service (intent/result protocol; fails closed if the journal is
    # unavailable). The CLI reuses the application's production composition so
    # it writes to the same verified journal with the same contract.
    audit_service = AuditSubsystem(
        builder=production_audit_builder(settings)
    ).service()
    if audit_service is None:
        print("ERROR: Audit subsystem unavailable. Provisioning aborted.", file=sys.stderr)
        raise SystemExit(1)

    service = UserService(
        users=user_repository,
        passwords=password_service,
        audit=audit_service,
        sessions=SessionService(
            users=user_repository,
            sessions=session_repository,
        ),
    )

    # Gather credentials interactively (never from command-line arguments).
    if user_id is None:
        user_id = _prompt_nonzero("Admin user ID: ")
    if username is None:
        username = _prompt_nonzero("Admin username: ")
    password = _confirm_password()

    # Attempt provisioning.
    try:
        result = service.provision_first_admin(
            user_id=user_id,
            username=username,
            password=password,
        )
    except AdminAlreadyExistsError:
        print("ERROR: An active Admin already exists. Provisioning refused.", file=sys.stderr)
        raise SystemExit(1) from None
    except UserAlreadyExistsError:
        print("ERROR: A user with that username or ID already exists.", file=sys.stderr)
        raise SystemExit(1) from None
    except PasswordTooShortError:
        print("ERROR: Password must be at least 8 characters.", file=sys.stderr)
        raise SystemExit(1) from None
    except UserValidationError as exc:
        print(f"ERROR: Invalid account details: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except UserAdministrationError:
        print("ERROR: The account details were rejected.", file=sys.stderr)
        raise SystemExit(1) from None
    except AuditUnavailableError:
        print("ERROR: Audit subsystem unavailable. Provisioning aborted.", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print("ERROR: Provisioning failed due to an internal error.", file=sys.stderr)
        raise SystemExit(1) from exc

    # Sanitized success output — never the password or hash.
    print("\nFirst Admin provisioned successfully.")
    print(f"  User ID: {result.user_id}")
    print(f"  Username: {result.username}")
    print(f"  Role: {result.role.value}")
    print(f"  Created at: {result.created_at.isoformat()}")
    print("\nIMPORTANT: Store these credentials securely. They are not recoverable.")
    print()
    return result


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``python -m src.user_provision``."""
    args = _parse_args(argv)

    try:
        provision_first_admin(
            user_id=args.user_id,
            username=args.username,
            database_url=args.database_url,
        )
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: Unexpected failure: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
