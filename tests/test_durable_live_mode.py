"""P2-5 — opt-in durable composition in the integration demo.

Covers exactly the wiring added by Step 25:

* Smoke mode (``run_smoke(..., durable=True)``) composing the real
  Step 13 engine with the P2-1 durable event store via the P2-2
  ``DurableAttendanceService``, with P2-3 cooldown recovery replayed
  **before** any frame is processed.
* Live mode (``run_live(..., config=LiveModeConfig(durable=True))``)
  wiring, tested hardware-free through injected camera/detector
  doubles — recovery must complete before any hardware acquisition
  and a journal-load or recovery failure must fail closed.

Everything runs on synthetic, deterministic data in temporary
directories. No camera, no model files, no real journal, no biometric
artifacts are ever touched; events carry only the documented five-key
privacy envelope.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.attendance import AttendanceConfig, EventType
from src.attendance import AttendanceEvent
from src.attendance_store import (
    AttendanceEventStore,
    AttendanceStoreInputError,
    AttendanceStorePersistenceError,
    CorruptAttendanceJournalError,
    UnsupportedJournalSchemaVersionError,
)
from src.config import AppSettings
from src.integration_demo import LiveModeConfig, run_live, run_smoke

_EMPLOYEE = "007"  # the exact smoke-fixture employee ID


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )


def _journal(settings: AppSettings) -> Path:
    return AttendanceEventStore(settings=settings).journal_path


def _fake_wall(base: datetime | None = None):
    """Deterministic timezone-aware UTC wall clock for durable runs."""
    t0 = base or datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)
    return lambda: t0


def _seed_event(
    settings: AppSettings,
    *,
    occurred_at: datetime,
    employee_id: str = _EMPLOYEE,
    event_id: str = "ev-seed",
) -> None:
    store = AttendanceEventStore(settings=settings)
    store.append(
        AttendanceEvent(
            event_id=event_id,
            employee_id=employee_id,
            event_type=EventType.CHECK_IN,
            occurred_at=occurred_at,
        )
    )


# ---------------------------------------------------------------------------
# 1. Default-off behavior: the journal is never touched
# ---------------------------------------------------------------------------


class TestFlagOffNeverTouchesTheJournal:
    def test_flag_off_smoke_never_loads_or_writes_the_journal(
        self, tmp_path: Path
    ) -> None:
        """Default invocation keeps the exact pre-P2-5 behavior."""
        settings = _settings(tmp_path)
        result = run_smoke(settings=settings)

        assert result.event_created is True  # sanity: the run itself ran
        assert result.durable_outcomes == ()
        assert result.durable_recovered_anchors == 0
        # No journal directory (and therefore no journal file) exists:
        # the store was never constructed and recovery never ran.
        assert not _journal(settings).parent.exists()
        assert not _journal(settings).exists()

    def test_flag_off_smoke_does_not_read_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recovery would be observable through a poisoned journal probe."""
        import src.attendance_recovery as recovery_mod

        settings = _settings(tmp_path)
        calls: list[Any] = []

        real_recover = recovery_mod.recover_cooldown_anchors

        def recording(**kwargs: Any):
            calls.append(kwargs)
            return real_recover(**kwargs)

        monkeypatch.setattr(
            recovery_mod, "recover_cooldown_anchors", recording
        )
        run_smoke(settings=settings)
        assert calls == []  # flag-off: no recovery call at all


# ---------------------------------------------------------------------------
# 2. Durable smoke run: exact composition semantics
# ---------------------------------------------------------------------------


class TestDurableSmokeHappyPath:
    def test_created_event_is_persisted_exactly_once(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        wall = _fake_wall()
        result = run_smoke(settings=settings, durable=True, wall_clock=wall)

        # The engine's own decision pattern is unchanged: one verified
        # event, later repeats suppressed.
        assert result.event_created is True
        assert result.event_employee_id == _EMPLOYEE
        assert result.event_occurred_at_iso == wall().isoformat()
        assert result.durable_recovered_anchors == 0
        assert result.durable_outcomes == (
            "no_event",
            "no_event",
            "persisted",
            "no_event",
            "no_event",
        )

        store = AttendanceEventStore(settings=settings)
        load = store.load_events()
        assert len(load.events) == 1
        stored = load.events[0]
        assert isinstance(stored, AttendanceEvent)
        assert stored.employee_id == _EMPLOYEE
        assert stored.event_type is EventType.CHECK_IN
        assert stored.occurred_at == wall()

    def test_persistence_status_is_labeled_in_the_attendance_outcome(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        result = run_smoke(
            settings=settings, durable=True, wall_clock=_fake_wall()
        )
        attendance_details = [
            o.detail for o in result.outcomes if o.stage.value == "attendance"
        ]
        assert any("durable: persisted" in d for d in attendance_details)
        # The human-readable line never carries biometric content —
        # statuses, IDs, and counts only.
        assert all("embedding" not in d and "score" not in d for d
                   in attendance_details)

    def test_journal_records_carry_only_the_privacy_envelope(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        run_smoke(settings=settings, durable=True, wall_clock=_fake_wall())
        lines = _journal(settings).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert set(record) == {
            "schema_version",
            "event_id",
            "employee_id",
            "event_type",
            "occurred_at",
        }

    def test_restart_suppresses_after_recovery(self, tmp_path: Path) -> None:
        """A fresh run over the persisted journal recovers the anchor.

        Simulates exactly one legitimate process restart: run one
        persists the event into a journal; that journal file is carried
        to a second run's (fresh, isolated) settings as-is. Recovery
        before processing must then suppress the would-be frame-3
        event (duplicate check-in exactly-once at the demo level) —
        and nothing new is appended.
        """
        first = _settings(tmp_path / "run1")
        second = _settings(tmp_path / "run2")
        wall = _fake_wall()
        run_smoke(settings=first, durable=True, wall_clock=wall)

        # Restart: the same journal (byte-for-byte) is now the second
        # run's starting history.
        _journal(second).parent.mkdir(parents=True, exist_ok=True)
        _journal(second).write_text(
            _journal(first).read_text(encoding="utf-8"), encoding="utf-8"
        )

        replay = run_smoke(settings=second, durable=True, wall_clock=wall)
        assert replay.event_created is False
        assert replay.suppressions == 3  # frame 3 + the two repeats
        assert replay.durable_recovered_anchors == 1
        assert replay.durable_outcomes == ("no_event",) * 5

        load = AttendanceEventStore(settings=second).load_events()
        assert len(load.events) == 1  # nothing was re-appended
        assert load.incomplete_trailing_record is False


# ---------------------------------------------------------------------------
# 3. Recovery-before-processing (durable smoke)
# ---------------------------------------------------------------------------


class TestRecoveryBeforeProcessing:
    @pytest.mark.parametrize(
        ("age_seconds", "expect_suppressed"),
        [((300.0 - 1.0), True), (300.0, False)],
        ids=["inside-cooldown", "at-boundary-expired"],
    )
    def test_cooldown_boundary(
        self, tmp_path: Path, age_seconds: float, expect_suppressed: bool
    ) -> None:
        """A journaled check-in inside the cooldown suppresses; at the
        exact boundary it has expired and the run creates its event.
        """
        settings = _settings(tmp_path)
        wall = _fake_wall()
        _seed_event(
            settings,
            occurred_at=wall() - timedelta(seconds=age_seconds),
        )
        result = run_smoke(settings=settings, durable=True, wall_clock=wall)

        if expect_suppressed:
            assert result.event_created is False
            assert result.suppressions == 3
            assert set(result.durable_outcomes) == {"no_event"}
        else:
            assert result.event_created is True
            assert result.durable_outcomes == (
                "no_event",
                "no_event",
                "persisted",
                "no_event",
                "no_event",
            )
            load = AttendanceEventStore(
                settings=settings
            ).load_events()
            assert len(load.events) == 2  # seed + the fresh check-in

    def test_pre_seeded_journal_is_not_re_appended(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        wall = _fake_wall()
        _seed_event(settings, occurred_at=wall() - timedelta(seconds=1))
        before = _journal(settings).read_text(encoding="utf-8")
        run_smoke(settings=settings, durable=True, wall_clock=wall)
        after = _journal(settings).read_text(encoding="utf-8")
        # Recovery read-only replay plus a suppressed run leaves the
        # journal byte-for-byte unchanged (no uncertain retry, no
        # duplicate suppression record).
        assert after == before


# ---------------------------------------------------------------------------
# 4. Persistence failures: four outcomes, no unsafe retries
# ---------------------------------------------------------------------------


class TestPersistenceFailureSurfaces:
    def test_rejected_persistence_is_reported_and_journal_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.attendance_store as store_mod

        settings = _settings(tmp_path)
        attempts: list[int] = []
        real_append = store_mod.AttendanceEventStore.append

        def one_then_reject(self: Any, event: Any) -> Path:
            attempts.append(1)
            raise AttendanceStoreInputError("rejected before any write")

        monkeypatch.setattr(store_mod.AttendanceEventStore, "append", one_then_reject)
        result = run_smoke(
            settings=settings, durable=True, wall_clock=_fake_wall()
        )

        assert attempts == [1]  # exactly one attempt — never retried
        assert result.durable_outcomes[2] == "persistence_rejected"
        assert any(
            "durable: persistence_rejected" in o.detail
            for o in result.outcomes
            if o.stage.value == "attendance"
        )
        # Input rejections occur pre-write: the journal is provably empty.
        load = AttendanceEventStore(settings=settings).load_events()
        assert len(load.events) == 0

    def test_unconfirmed_persistence_is_reported_and_never_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.attendance_store as store_mod

        settings = _settings(tmp_path)
        attempts: list[int] = []

        def one_then_fail(self: Any, event: Any) -> Path:
            attempts.append(1)
            raise AttendanceStorePersistenceError("simulated I/O failure")

        monkeypatch.setattr(store_mod.AttendanceEventStore, "append", one_then_fail)
        result = run_smoke(
            settings=settings, durable=True, wall_clock=_fake_wall()
        )

        assert attempts == [1]  # one uncertain append; never re-appended
        assert result.durable_outcomes[2] == "persistence_unconfirmed"
        # The run itself still completes honestly (the engine already
        # suppressed the repeats; composition does not fabricate state).
        assert result.suppressions == 2


# ---------------------------------------------------------------------------
# 5. Durable smoke fails closed on a corrupt journal
# ---------------------------------------------------------------------------


class TestDurableSmokeFailsClosed:
    def test_corrupt_line_aborts_before_processing(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        journal = _journal(settings)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text("not valid json\n", encoding="utf-8")

        with pytest.raises(CorruptAttendanceJournalError):
            run_smoke(settings=settings, durable=True, wall_clock=_fake_wall())

    def test_unknown_schema_version_aborts_before_processing(
        self, tmp_path: Path
    ) -> None:
        settings = _settings(tmp_path)
        journal = _journal(settings)
        journal.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 2,
            "event_id": "ev-future",
            "employee_id": _EMPLOYEE,
            "event_type": "check_in",
            "occurred_at": "2026-10-01T07:00:00+00:00",
        }
        journal.write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )

        with pytest.raises(UnsupportedJournalSchemaVersionError):
            run_smoke(settings=settings, durable=True, wall_clock=_fake_wall())


# ---------------------------------------------------------------------------
# 6. Durable live wiring (hardware-free: injected camera/detector doubles)
# ---------------------------------------------------------------------------


def _install_demo_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    detector_events: list[str],
    camera_events: list[str],
) -> None:
    """Make live prerequisites claim both models but replace every
    hardware boundary with inert doubles (no model load, no device)."""
    import src.camera as camera_mod
    import src.integration_demo as demo_mod

    prereqs = demo_mod._LivePrereq(
        yunet_available=True,
        yunet_path=Path("fake-yunet.onnx"),
        sface_available=False,  # partial demo: no embedder is ever built
        sface_path=Path("fake-sface.onnx"),
    )
    monkeypatch.setattr(demo_mod, "live_prerequisites", lambda **_kw: prereqs)

    class FakeDetector:
        def load(self) -> None:
            detector_events.append("loaded")

        def detect(self, frame: Any) -> list[object]:
            return []

    class FakeCamera:
        def __init__(self, config: Any) -> None:
            camera_events.append("constructed")

        @property
        def is_open(self) -> bool:
            return True

        def open(self) -> None:
            camera_events.append("opened")

        def read_frame(self) -> tuple[bool, None]:
            camera_events.append("read")
            return (False, None)  # stops the loop cleanly, frame-free

        def release(self) -> None:
            camera_events.append("released")

    monkeypatch.setattr(demo_mod, "FaceDetector", FakeDetector)
    monkeypatch.setattr(camera_mod, "CameraService", FakeCamera)


class TestDurableLiveWiring:
    def test_durable_live_recovers_before_any_hardware(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A corrupt journal fails closed before the detector/camera.

        If recovery ran after hardware acquisition the doubles would
        record their events; the empty recordings prove the ordering.
        """
        detector_events: list[str] = []
        camera_events: list[str] = []
        _install_demo_doubles(
            monkeypatch,
            detector_events=detector_events,
            camera_events=camera_events,
        )
        settings = _settings(tmp_path)
        journal = _journal(settings)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text("corrupt\n", encoding="utf-8")

        with pytest.raises(CorruptAttendanceJournalError):
            run_live(
                settings=settings,
                config=LiveModeConfig(open_window=False, durable=True),
            )
        assert detector_events == []
        assert camera_events == []

    def test_durable_live_clean_run_reports_recovery_and_writes_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        detector_events: list[str] = []
        camera_events: list[str] = []
        _install_demo_doubles(
            monkeypatch,
            detector_events=detector_events,
            camera_events=camera_events,
        )
        settings = _settings(tmp_path)
        frames = run_live(
            settings=settings,
            config=LiveModeConfig(open_window=False, durable=True),
        )
        assert frames == 0
        assert detector_events == ["loaded"]
        assert "opened" in camera_events  # durable failure-free path runs
        assert "released" in camera_events  # resources always cleaned up

        out = capsys.readouterr().out
        assert (
            "durable mode: cooldown recovery replayed before any event "
            "processing (0 anchor(s) restored, 0 expired, 0 clamped, "
            "torn tail: no)" in out
        )
        # Missing journal normalized to empty history; nothing written.
        assert not _journal(settings).exists()

    def test_flag_off_live_never_loads_or_writes_the_journal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        detector_events: list[str] = []
        camera_events: list[str] = []
        _install_demo_doubles(
            monkeypatch,
            detector_events=detector_events,
            camera_events=camera_events,
        )
        settings = _settings(tmp_path)
        frames = run_live(
            settings=settings,
            config=LiveModeConfig(open_window=False),
        )
        assert frames == 0
        out = capsys.readouterr().out
        assert "durable mode" not in out
        assert not _journal(settings).exists()


# ---------------------------------------------------------------------------
# 7. P2-6 S4 wiring: single-snapshot startup recovery + journal-backed
#    session lookup on the durable engine
# ---------------------------------------------------------------------------


class TestP26StartupWiring:
    def test_smoke_durable_engine_gets_journal_backed_lookup_and_session_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable engine is constructed with the journal-backed
        open-session lookup, and startup recovery restores both check-in
        anchors and open-session markers from the seeded journal."""
        import src.attendance as attendance_mod

        settings = _settings(tmp_path)
        wall = _fake_wall()
        _seed_event(
            settings, occurred_at=wall() - timedelta(seconds=30)
        )
        real_engine = attendance_mod.AttendanceEngine

        class SpyEngine(real_engine):
            instances: list["SpyEngine"] = []

            def __init__(self, **kwargs: Any) -> None:
                self.init_kwargs = kwargs
                self.session_restores: list[tuple[str, float]] = []
                super().__init__(**kwargs)
                type(self).instances.append(self)

            def restore_open_session(
                self, employee_id: str, *, elapsed_seconds: float
            ) -> bool:
                self.session_restores.append(
                    (employee_id, elapsed_seconds)
                )
                return super().restore_open_session(
                    employee_id, elapsed_seconds=elapsed_seconds
                )

        monkeypatch.setattr(attendance_mod, "AttendanceEngine", SpyEngine)
        run_smoke(settings=settings, durable=True, wall_clock=wall)
        assert len(SpyEngine.instances) == 1
        spy = SpyEngine.instances[0]
        # The checkout hook is the journal-backed lookup (callable).
        hook = spy.init_kwargs.get("open_session_lookup")
        assert callable(hook)
        # Latest persisted CHECK_IN → the hook says a session is open.
        assert hook(_EMPLOYEE) is True
        assert hook("NO-SUCH") is False
        # Startup recovery restored the open-session marker too.
        assert spy.session_restores == [(_EMPLOYEE, 30.0)]

    def test_startup_recovery_loads_the_journal_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One validated journal snapshot backs both replay phases.

        The startup load count is exactly one: the session hook is lazy
        (never consulted during a check-in-only smoke run) and nothing
        else reads the journal at startup.
        """
        import src.attendance_store as store_mod

        settings = _settings(tmp_path)
        wall = _fake_wall()
        _seed_event(
            settings, occurred_at=wall() - timedelta(seconds=30)
        )
        loads: list[int] = []
        real_load = store_mod.AttendanceEventStore.load_events

        def counting(self: Any):
            loads.append(1)
            return real_load(self)

        monkeypatch.setattr(
            store_mod.AttendanceEventStore, "load_events", counting
        )
        result = run_smoke(
            settings=settings, durable=True, wall_clock=wall
        )
        assert loads == [1]  # one snapshot, both phases
        assert result.durable_recovered_anchors == 1

    def test_run_live_recovery_finishes_before_hardware_initialization(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Call-order proof: startup recovery precedes detector load and
        camera acquire even on a journal-free durable run."""
        import src.attendance_recovery as recovery_mod

        detector_events: list[str] = []
        camera_events: list[str] = []
        _install_demo_doubles(
            monkeypatch,
            detector_events=detector_events,
            camera_events=camera_events,
        )
        order: list[str] = []
        real_recover = recovery_mod.recover_attendance_startup_state

        def recording(*args: Any, **kwargs: Any):
            order.append("recovery")
            return real_recover(*args, **kwargs)

        monkeypatch.setattr(
            recovery_mod, "recover_attendance_startup_state", recording
        )
        settings = _settings(tmp_path)
        run_live(
            settings=settings,
            config=LiveModeConfig(open_window=False, durable=True),
        )
        assert order == ["recovery"]
        # Recovery ran before the detector was loaded and the camera
        # opened (hardware events exist only after it).
        assert detector_events == ["loaded"]
        assert camera_events[0] == "constructed"
        assert "opened" in camera_events

    def test_durable_live_fails_closed_on_corrupt_journal_with_session_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corruption aborts before attendance processing and camera —
        never degraded and never re-labeled as a closed session."""
        detector_events: list[str] = []
        camera_events: list[str] = []
        _install_demo_doubles(
            monkeypatch,
            detector_events=detector_events,
            camera_events=camera_events,
        )
        settings = _settings(tmp_path)
        journal = _journal(settings)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text("corrupt\n", encoding="utf-8")

        with pytest.raises(CorruptAttendanceJournalError):
            run_smoke(settings=settings, durable=True, wall_clock=_fake_wall())
        assert detector_events == []
        assert camera_events == []

    def test_durable_live_reports_single_snapshot_session_recovery(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import src.attendance_store as store_mod

        detector_events: list[str] = []
        camera_events: list[str] = []
        _install_demo_doubles(
            monkeypatch,
            detector_events=detector_events,
            camera_events=camera_events,
        )
        settings = _settings(tmp_path)
        loads: list[int] = []
        real_load = store_mod.AttendanceEventStore.load_events

        def counting(self: Any):
            loads.append(1)
            return real_load(self)

        monkeypatch.setattr(
            store_mod.AttendanceEventStore, "load_events", counting
        )
        run_live(
            settings=settings,
            config=LiveModeConfig(open_window=False, durable=True),
        )
        out = capsys.readouterr().out
        # The established P2-5 wording is preserved; the S4 line names
        # the same-snapshot session recovery.
        assert (
            "durable mode: cooldown recovery replayed before any event "
            "processing (0 anchor(s) restored, 0 expired, 0 clamped, "
            "torn tail: no)" in out
        )
        assert (
            "durable mode: session state recovered from the same "
            "validated snapshot (0 open session(s) restored, "
            "0 checkout anchor(s) restored, 0 expired)" in out
        )
        # Startup loaded the journal exactly once.
        assert loads == [1]

    def test_no_auto_checkout_trigger_journal_stays_check_in_only(
        self, tmp_path: Path
    ) -> None:
        """Ordinary verified attendance keeps creating CHECK_IN events
        only; S4 introduces no checkout caller of any kind."""
        settings = _settings(tmp_path)
        result = run_smoke(
            settings=settings, durable=True, wall_clock=_fake_wall()
        )
        assert result.event_created is True
        load = AttendanceEventStore(settings=settings).load_events()
        assert [e.event_type for e in load.events] == [
            EventType.CHECK_IN
        ]
