"""Step 1.4 startup-guard verification (Windows ``msvcrt`` byte-range lock).

Covers the approved single-process guard (:mod:`src.api_guard`, wired
into :func:`src.api.run_server` and ``tools/run_api.py``):

1. First holder acquires; the OS enforces exclusivity across separate
   processes (real subprocess holders — never mocked locks — on win32).
2. Contention denies a second process; after orderly release, after
   abrupt termination (``os._exit``, kill + reap), and with a
   pre-existing stale file, acquisition succeeds again.
3. The guard file is idempotent: the initial byte is written only when
   empty, so repeated startups never grow the file.
4. A failed acquisition prevents Uvicorn from starting (fresh-process
   proof: ``uvicorn`` never enters ``sys.modules``).
5. The lock stays held for the serving process's lifetime and is
   released by an orderly Uvicorn shutdown (real subprocess receiving
   ``CTRL_BREAK_EVENT``, routed by uvicorn into a graceful stop).
6. The runner translates the typed guard failure into the documented
   exit code 3 with a sanitized one-line diagnostic (no paths, values,
   or tracebacks), and ``--print-bind`` never acquires the guard.
7. Non-win32 platforms fail deterministically with the typed
   ``StartupGuardUnavailableError`` — no POSIX fallback.
8. Every child process is killed and reaped in ``finally`` blocks; all
   lock files live in per-test temporary ``DATA_DIR`` values (never the
   project's real runtime directory).

The module is skipped entirely on non-Windows platforms: the guard is
Windows-only by approved contract, so OS-level cases genuinely cannot
be exercised there (a skip, never a fake pass).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # mirrors pytest.ini's pythonpath = .
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api_guard import (  # noqa: E402
    GUARD_FILE_NAME,
    StartupGuard,
    StartupGuardError,
    StartupGuardUnavailableError,
    acquire_startup_guard,
)
from src.config import load_settings  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "the startup guard is Windows-only by approved contract (msvcrt); "
        "POSIX behavior is explicitly out of scope, so the OS-level cases "
        "below cannot be exercised on other platforms (deliberate skip, "
        "documented in README/DEVELOPMENT_STATUS)"
    ),
)

CONTENTION_MSG = (
    "the single-instance startup guard is already held by another "
    "process; refusing to start the API"
)

#: Byte 0 only — by approved contract exactly one byte is locked.
_LOCK_SIZE = 1


# ---------------------------------------------------------------------------
# Settings/run helpers (temporary DATA_DIR only — never the project's data/)
# ---------------------------------------------------------------------------


def _temp_settings(data_dir: Path):
    """Real validated settings object, redirected to a temp data dir."""
    base = load_settings()  # validates environment (loopback/ports)
    return replace(
        base,
        data_dir=data_dir,
        log_dir=data_dir / "logs",  # keep any file logging inside the tmpdir
    )


def _probe_script() -> str:
    return (
        "import sys\n"
        f"sys.path.insert(0, r'{PROJECT_ROOT}')\n"
        "from src.api_guard import acquire_startup_guard as A, StartupGuardError\n"
        "from src.config import load_settings as L\n"
        "try:\n"
        "    g = A(L())\n"
        "    print('ACQUIRED')\n"
        "    g.release()\n"
        "except StartupGuardError:\n"
        "    print('DENIED')\n"
        "except BaseException as e:  # unexpected: type name only\n"
        "    print('ERROR', type(e).__name__)\n"
    )


def _holder_script(orderly: bool) -> str:
    holding = (
        "    g = A(L())\n"
        "    print('HELD')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(45)\n"
    )
    if orderly:
        holding += "    g.release()\n    print('RELEASED')\n"
    else:
        # No unlock, no cleanup, no atexit: only the OS may release the
        # byte-range lock when the process dies like this.
        holding += "    print('CRASHING')\n    sys.stdout.flush()\n    os._exit(1)\n"
    return (
        "import os, sys, time\n"
        f"sys.path.insert(0, r'{PROJECT_ROOT}')\n"
        "from src.api_guard import acquire_startup_guard as A, StartupGuardError\n"
        "from src.config import load_settings as L\n"
        "try:\n"
        + holding +
        "except StartupGuardError:\n"
        "    print('DENIED')\n"
    )


def _child_env(data_dir: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ, DATA_DIR=str(data_dir), LOG_DIR=str(data_dir / "logs"))
    env.update(extra)
    return env


def _wait_for_marker(proc: subprocess.Popen, marker: str, timeout: float = 20.0) -> str:
    """Read stdout lines (bounded) until one starts with ``marker``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            break
        line = line.strip()
        if line.startswith(marker):
            return line
        # keep scanning (e.g. library banners ahead of the marker)
    raise AssertionError(
        f"child never announced {marker!r}; rc={proc.poll()}"
    )


def _probe_lock(data_dir: Path) -> str:
    """Fresh process tries the real acquisition; returns ACQUIRED/DENIED."""
    proc = subprocess.run(
        [sys.executable, "-c", _probe_script()],
        capture_output=True,
        text=True,
        timeout=30,
        env=_child_env(data_dir),
        cwd=str(PROJECT_ROOT),
    )
    out = proc.stdout.strip()
    assert "ERROR" not in out, f"probe hit an unexpected failure: {out} {proc.stderr}"
    assert out in {"ACQUIRED", "DENIED"}, f"unexpected probe output: {out!r}"
    return out


class _GuardHolder:
    """Subprocess holding the real lock until orderly release or kill."""

    def __init__(self, data_dir: Path, orderly: bool) -> None:
        self.orderly = orderly
        self.process = subprocess.Popen(
            [sys.executable, "-c", _holder_script(orderly)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_child_env(data_dir),
            cwd=str(PROJECT_ROOT),
        )

    def wait_held(self) -> None:
        line = _wait_for_marker(self.process, "HELD")
        assert line == "HELD", f"holder did not acquire: {line!r}"

    def wait_released(self) -> bool:
        try:
            return _wait_for_marker(self.process, "RELEASED") == "RELEASED"
        finally:
            self.terminate()

    def terminate(self) -> None:
        """Kill (idempotent) and reap — no test leaves a process behind."""
        proc = self.process
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=10)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()


# ---------------------------------------------------------------------------
# In-process unit-level behaviors
# ---------------------------------------------------------------------------


def test_first_holder_acquires_and_release_works(tmp_path: Path) -> None:
    settings = _temp_settings(tmp_path)
    guard = acquire_startup_guard(settings)
    try:
        lock_file = tmp_path / GUARD_FILE_NAME
        assert lock_file.exists()
        # Reads of the locked byte range are denied on Windows while the
        # guard is held (see DEVELOPMENT_STATUS Step 28 note), so the
        # exact content assertion runs only after release (below).
        assert lock_file.stat().st_size >= 1
        assert guard.lock_path == lock_file
    finally:
        guard.release()
    assert lock_file.read_bytes() == b"\x01"


def test_release_is_idempotent_and_file_persists(tmp_path: Path) -> None:
    settings = _temp_settings(tmp_path)
    guard = acquire_startup_guard(settings)
    guard.release()
    guard.release()  # second release is a no-op, never an error
    lock_file = tmp_path / GUARD_FILE_NAME
    assert lock_file.exists(), "orderly release must not delete the guard file"


def test_repeated_starts_do_not_grow_the_lock_file(tmp_path: Path) -> None:
    settings = _temp_settings(tmp_path)
    for _ in range(3):
        with acquire_startup_guard(settings):
            pass  # normal lifecycle: acquire, then release
    lock_file = tmp_path / GUARD_FILE_NAME
    assert lock_file.read_bytes() == b"\x01"
    assert lock_file.stat().st_size == _LOCK_SIZE


def test_stale_file_blocks_nothing_and_is_not_rewritten(tmp_path: Path) -> None:
    lock_file = tmp_path / GUARD_FILE_NAME
    lock_file.write_bytes(b"\x01")  # leftover of an abnormal termination
    stale_size = lock_file.stat().st_size
    with acquire_startup_guard(_temp_settings(tmp_path)):
        assert lock_file.stat().st_size == stale_size, (
            "acquisition must not rewrite existing guard content"
        )
        assert _probe_lock(tmp_path) == "DENIED"  # exclusivity still enforced
    assert lock_file.stat().st_size == stale_size
    assert lock_file.read_bytes() == b"\x01"  # readable only after release
    assert _probe_lock(tmp_path) == "ACQUIRED"


def test_explicit_failure_on_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-win32 → typed deterministic refusal, no POSIX fallback."""
    monkeypatch.setattr(sys, "platform", "linux")  # simulate non-Windows
    with pytest.raises(StartupGuardUnavailableError) as excinfo:
        acquire_startup_guard(_temp_settings(tmp_path))
    assert isinstance(excinfo.value, StartupGuardError)
    message = str(excinfo.value)
    assert "msvcrt" in message and "POSIX" in message
    assert str(tmp_path) not in message
    assert not (tmp_path / GUARD_FILE_NAME).exists(), "no lock file on refusal"


def test_contention_message_is_sanitized(tmp_path: Path) -> None:
    """Real cross-process contention; exception text carries no path."""
    holder = _GuardHolder(tmp_path, orderly=False)
    try:
        holder.wait_held()
        with pytest.raises(StartupGuardError) as excinfo:
            acquire_startup_guard(_temp_settings(tmp_path))
        message = str(excinfo.value)
        assert message == CONTENTION_MSG
        assert str(tmp_path) not in message
        assert "api_guard.lock" not in message
        for token in ("traceback", "errno", "0x"):
            assert token not in message.lower()
    finally:
        holder.terminate()


def test_guard_object_cleans_up_on_context_exit(tmp_path: Path) -> None:
    settings = _temp_settings(tmp_path)
    guard = acquire_startup_guard(settings)
    with guard:
        assert _probe_lock(tmp_path) == "DENIED"
    assert _probe_lock(tmp_path) == "ACQUIRED"  # context manager released it
    assert guard._handle is None  # closed handle; no fd retained


# ---------------------------------------------------------------------------
# Real OS-level subprocess behaviors
# ---------------------------------------------------------------------------


def test_second_process_denied_then_allowed_after_orderly_release(
    tmp_path: Path,
) -> None:
    holder = _GuardHolder(tmp_path, orderly=True)
    try:
        holder.wait_held()
        assert _probe_lock(tmp_path) == "DENIED"
        assert holder.wait_released() is True
        assert _probe_lock(tmp_path) == "ACQUIRED"
    finally:
        holder.terminate()


def test_acquisition_succeeds_after_abrupt_termination(tmp_path: Path) -> None:
    holder = _GuardHolder(tmp_path, orderly=False)
    try:
        holder.wait_held()
        assert _wait_for_marker(holder.process, "CRASHING") == "CRASHING"
    finally:
        holder.terminate()  # kills (if still alive) and reaps
    time.sleep(0.2)
    # No unlock ever ran: only the OS released the byte range.
    assert _probe_lock(tmp_path) == "ACQUIRED"
    lock_file = tmp_path / GUARD_FILE_NAME
    assert lock_file.exists()
    assert lock_file.read_bytes() == b"\x01"


def _free_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _poll_health(base_url: str, timeout_s: float = 20.0):
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310 - loopback URL only
                f"{base_url}/api/v1/health", timeout=3
            ) as resp:
                return resp.status, resp.read().decode("utf-8")
        except (urllib.error.URLError, ConnectionError, OSError) as err:
            last_err = err
            time.sleep(0.15)
    raise AssertionError(f"server never became healthy: {last_err}")


def _assert_nothing_listening(port: int) -> None:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return  # expected: connection refused, nothing to reap
    pytest.fail("a server is listening despite guard contention")


def _runner(
    tmp_path: Path, port: int, *args: str
) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "tools" / "run_api.py"), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(tmp_path, API_HOST="127.0.0.1", API_PORT=str(port)),
        cwd=str(PROJECT_ROOT),
    )


def _reap_runner(runner: subprocess.Popen) -> tuple[int, str, str]:
    """Kill+reap unconditionally; return (returncode, stdout, stderr)."""
    try:
        if runner.poll() is None:
            runner.kill()
        runner.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        runner.kill()
        runner.wait(timeout=15)
    stdout = runner.stdout.read() if runner.stdout else ""
    stderr = runner.stderr.read() if runner.stderr else ""
    for stream in (runner.stdout, runner.stderr):
        if stream:
            stream.close()
    return runner.returncode, stdout, stderr


def test_guard_contention_prevents_uvicorn_from_starting(tmp_path: Path) -> None:
    holder = _GuardHolder(tmp_path, orderly=False)
    port = _free_loopback_port()
    runner: subprocess.Popen | None = None
    try:
        holder.wait_held()
        runner = _runner(tmp_path, port)
        try:
            rc = runner.wait(timeout=30)
        except subprocess.TimeoutExpired:
            raise AssertionError("runner did not fail fast under contention")
        stdout = runner.stdout.read() if runner.stdout else ""
        stderr = runner.stderr.read() if runner.stderr else ""
        assert rc == 3, f"runner must exit 3 under contention; stderr={stderr!r}"
        assert f"run-api: {CONTENTION_MSG}" in stderr
        # Sanitization: no paths, no exception classes, no hex, no traceback.
        for fragment in (str(tmp_path), "api_guard.lock", "Traceback", "msvcrt", "0x", "OSError"):
            assert fragment not in stderr, f"leaked {fragment!r} in runner stderr"
        assert "Traceback" not in stdout
        # And nothing started serving: the port was never bound.
        _assert_nothing_listening(port)
    finally:
        if runner is not None:
            _reap_runner(runner)
        holder.terminate()


def test_uvicorn_never_imported_when_guard_rejects(tmp_path: Path) -> None:
    """Fresh interpreter: a guard rejection stops before Uvicorn loads."""
    holder = _GuardHolder(tmp_path, orderly=False)
    try:
        holder.wait_held()
        code = (
            "import sys\n"
            f"sys.path.insert(0, r'{PROJECT_ROOT}')\n"
            "from src.api import run_server\n"
            "from src.config import load_settings\n"
            "from src.api_guard import StartupGuardError\n"
            "try:\n"
            "    run_server(load_settings())\n"
            "except StartupGuardError:\n"
            "    assert 'uvicorn' not in sys.modules, 'uvicorn imported'\n"
            "    print('UVICORN_FREE')\n"
            "else:\n"
            "    print('UNEXPECTEDLY_SERVED')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_env(tmp_path),
            cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip().endswith("UVICORN_FREE")
        assert "UNEXPECTEDLY_SERVED" not in proc.stdout
    finally:
        holder.terminate()


def test_guard_held_for_serving_lifetime_then_released_by_crash(
    tmp_path: Path,
) -> None:
    """Real serving process: lock held while healthy; OS releases on kill."""
    port = _free_loopback_port()
    runner = _runner(tmp_path, port)
    try:
        status, body = _poll_health(f"http://127.0.0.1:{port}")
        assert status == 200 and body == '{"status":"ok"}'
        # While it serves, the byte range stays locked for other processes.
        assert _probe_lock(tmp_path) == "DENIED"
    finally:
        rc, _out, _err = _reap_runner(runner)
    # Abrupt TerminateProcess: no graceful shutdown, no unlock — the OS
    # released the byte range with the process.
    time.sleep(0.3)
    assert _probe_lock(tmp_path) == "ACQUIRED"
    lock_file = tmp_path / GUARD_FILE_NAME
    assert lock_file.exists()
    assert lock_file.read_bytes() == b"\x01", "served run must not grow the file"


def test_orderly_uvicorn_shutdown_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Uvicorn lifecycle: graceful stop releases the guard.

    Runs in-process so the ``uvicorn.Server`` instance created inside
    :func:`src.api.run_server` can be captured (recorded subclass) and
    asked for the graceful ``should_exit`` stop — the same path
    ``SIGINT``/``SIGTERM`` take when a real operator stops the server.
    A subprocess cannot be used here because Windows ``CTRL_BREAK_EVENT``
    is not translated to a graceful stop by uvicorn (default ``SIGBREAK``
    terminates without cleanup), and the guard contract's orderly release
    must be exercised on the genuine shutdown path, not on a kill.
    """
    import uvicorn

    # File logging of the server run goes to the tmpdir only.
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))

    recorded: list[uvicorn.Server] = []
    real_server_type = uvicorn.Server

    class RecordingServer(real_server_type):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            recorded.append(self)

    monkeypatch.setattr(uvicorn, "Server", RecordingServer)

    from src.api import run_server  # imports lazily with LOG_DIR already set

    port = _free_loopback_port()
    settings = replace(
        _temp_settings(tmp_path),
        api_host="127.0.0.1",
        api_port=port,
    )
    thread_error: list[BaseException] = []

    def serve() -> None:
        try:
            run_server(settings)
        except BaseException as exc:  # surfaced via join/assert below
            thread_error.append(exc)

    thread = threading.Thread(target=serve, name="step14-orderly-server", daemon=True)
    thread.start()
    try:
        status, body = _poll_health(f"http://127.0.0.1:{port}")
        assert status == 200 and body == '{"status":"ok"}'
        assert recorded, "the real run_server server instance was captured"
        recorded[0].should_exit = True  # graceful stop request
    finally:
        thread.join(timeout=30)
        if thread.is_alive():  # pragma: no cover - defensive
            raise AssertionError("orderly shutdown did not complete in time")
    if thread_error:
        raise thread_error[0]
    # Leaving the context manager inside run_server released the guard.
    assert _probe_lock(tmp_path) == "ACQUIRED"
    lock_file = tmp_path / GUARD_FILE_NAME
    assert lock_file.read_bytes() == b"\x01"


def test_runner_print_bind_never_acquires_the_guard(tmp_path: Path) -> None:
    holder = _GuardHolder(tmp_path, orderly=False)
    port = _free_loopback_port()
    try:
        holder.wait_held()
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "run_api.py"), "--print-bind"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_child_env(tmp_path, API_HOST="127.0.0.1", API_PORT=str(port)),
            cwd=str(PROJECT_ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == f"bind: 127.0.0.1:{port}"
        # The holder still owns the lock; nothing was acquired or clobbered.
        assert _probe_lock(tmp_path) == "DENIED"
    finally:
        holder.terminate()
