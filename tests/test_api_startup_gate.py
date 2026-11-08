"""Offline G2 startup-wiring tests (API seam + runner exit codes).

No database, socket, or real Uvicorn server is involved: the startup guard and
``uvicorn.Server`` are recording fakes, and the runner tests monkeypatch
``src.api.run_server``. Together these pin the approved startup order — guard →
G2 → Uvicorn — plus the refusal behavior and exit code 4.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import uvicorn

import src.api as api_module
import src.api_guard as guard_module
from src.api_guard import StartupGuardError
from src.config import AppSettings
from src.startup_schema import MIGRATIONS_PENDING, StartupSchemaError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # mirrors pytest.ini's pythonpath = .
    sys.path.insert(0, str(PROJECT_ROOT))

APP_DSN = "postgresql://app_user:app-secret@localhost:5432/attendance"
TEST_DSN = "postgresql://tester:test-secret@127.0.0.1:5432/attendance_test"

_RUNNER_PATH = PROJECT_ROOT / "tools" / "run_api.py"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path,
        log_dir=tmp_path / "logs",
        database_url=APP_DSN,
        test_database_url=TEST_DSN,
    )


class _RecordingGuard:
    """Stand-in for the real Windows guard: records enter/exit only."""

    def __init__(self, tmp_path: Path, events: list[str]) -> None:
        self.lock_path = tmp_path / "api_guard.lock"
        self._events = events

    def __enter__(self):
        self._events.append("guard_enter")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._events.append("guard_exit")
        return False


def _install_fakes(monkeypatch, tmp_path: Path) -> list[str]:
    events: list[str] = []

    monkeypatch.setattr(
        guard_module,
        "acquire_startup_guard",
        lambda settings: _RecordingGuard(tmp_path, events),
    )

    class RecordingServer:
        def __init__(self, config) -> None:
            events.append("uvicorn_init")
            self.config = config

        def run(self) -> None:
            events.append("uvicorn_run")

    monkeypatch.setattr(uvicorn, "Server", RecordingServer)
    return events


def _load_runner():
    """Load ``tools/run_api.py`` as a module (it inserts PROJECT_ROOT itself)."""
    spec = importlib.util.spec_from_file_location("run_api_under_test", _RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Startup order and refusal in src.api.run_server
# ---------------------------------------------------------------------------


def test_guard_then_verification_then_uvicorn(tmp_path: Path, monkeypatch) -> None:
    events = _install_fakes(monkeypatch, tmp_path)

    def verifier(settings: AppSettings) -> None:
        events.append("verify")

    api_module.run_server(_settings(tmp_path), startup_verifier=verifier)
    assert events == [
        "guard_enter",
        "verify",
        "uvicorn_init",
        "uvicorn_run",
        "guard_exit",
    ]


def test_refusal_prevents_uvicorn_and_releases_the_guard(
    tmp_path: Path, monkeypatch
) -> None:
    events = _install_fakes(monkeypatch, tmp_path)

    def verifier(settings: AppSettings) -> None:
        events.append("verify")
        raise StartupSchemaError(MIGRATIONS_PENDING, "apply the shipped migrations")

    with pytest.raises(StartupSchemaError) as excinfo:
        api_module.run_server(_settings(tmp_path), startup_verifier=verifier)

    assert excinfo.value.code == MIGRATIONS_PENDING
    assert events == ["guard_enter", "verify", "guard_exit"]
    assert "uvicorn_init" not in events and "uvicorn_run" not in events


def test_production_default_uses_the_real_verifier(
    tmp_path: Path, monkeypatch
) -> None:
    """No seam → the real G2 verifier runs (no silent skip is possible)."""
    events = _install_fakes(monkeypatch, tmp_path)
    calls: list[AppSettings] = []

    def recorded(settings: AppSettings) -> None:
        calls.append(settings)
        events.append("verify")

    monkeypatch.setattr("src.startup_schema.verify_startup_schema", recorded)
    settings = _settings(tmp_path)
    api_module.run_server(settings)
    assert calls == [settings]
    assert events == [
        "guard_enter",
        "verify",
        "uvicorn_init",
        "uvicorn_run",
        "guard_exit",
    ]


def test_create_app_stays_database_free(monkeypatch) -> None:
    def forbidden(*args, **kwargs):  # pragma: no cover - tripwire
        raise AssertionError("create_app must not open a connection")

    monkeypatch.setattr("src.db._connect", forbidden)
    app = api_module.create_app()
    assert app is not None


# ---------------------------------------------------------------------------
# Runner exit codes and sanitized diagnostics
# ---------------------------------------------------------------------------


def test_schema_refusal_exits_4_with_sanitized_stderr(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "8000")

    def refusing(settings) -> None:
        raise StartupSchemaError(
            MIGRATIONS_PENDING, "apply the shipped migrations before starting the API"
        )

    monkeypatch.setattr("src.api.run_server", refusing)
    rc = _load_runner().main([])
    captured = capsys.readouterr()

    assert rc == 4
    assert captured.out == ""
    assert "run-api: MIGRATIONS_PENDING:" in captured.err
    assert "apply the shipped migrations" in captured.err
    for fragment in (
        "postgresql://",
        "app-secret",
        "test-secret",
        "Traceback",
        "psycopg",
    ):
        assert fragment not in captured.err


def test_guard_refusal_still_exits_3(monkeypatch, capsys) -> None:
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "8000")

    def refusing(settings) -> None:
        raise StartupGuardError("the guard is held")

    monkeypatch.setattr("src.api.run_server", refusing)
    rc = _load_runner().main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert captured.out == ""
    assert "run-api: the guard is held" in captured.err


def test_print_bind_skips_verification_and_serving(monkeypatch, capsys) -> None:
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "8022")

    def must_not_run(settings) -> None:  # pragma: no cover - tripwire
        raise AssertionError("run_server must not be called for --print-bind")

    monkeypatch.setattr("src.api.run_server", must_not_run)
    rc = _load_runner().main(["--print-bind"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "bind: 127.0.0.1:8022"
    assert captured.err == ""
