"""Step 02 configuration verification.

Maps to the Step 02 test requirements:

1. Configuration can be imported.
2. Important paths resolve correctly (including from a foreign cwd).
3. Expected configuration values are available and immutable.
4. Environment overrides work where env vars are intentionally supported.
5. Invalid configuration values are handled appropriately.
6. Importing configuration performs no side effects.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

from src.config import (
    APP_NAME,
    ATTENDANCE,
    CAMERA,
    ENROLLMENT,
    FACE_PROCESSING,
    SETTINGS,
    AppSettings,
    AttendanceConfig,
    CameraConfig,
    ConfigError,
    EnrollmentConfig,
    FaceProcessingConfig,
    PROJECT_ROOT as CONFIG_PROJECT_ROOT,
    ensure_directories,
    load_settings,
)


# ---------------------------------------------------------------------------
# 1. Configuration can be imported
# ---------------------------------------------------------------------------


def test_config_imports_with_core_surface() -> None:
    assert APP_NAME == "AI-Powered Visual Attendance System"
    assert isinstance(SETTINGS, AppSettings)
    assert isinstance(CAMERA, CameraConfig)
    assert isinstance(FACE_PROCESSING, FaceProcessingConfig)
    assert isinstance(ENROLLMENT, EnrollmentConfig)


# ---------------------------------------------------------------------------
# 2. Paths resolve correctly
# ---------------------------------------------------------------------------


def test_project_root_is_repository_root() -> None:
    assert CONFIG_PROJECT_ROOT == PROJECT_ROOT
    assert (CONFIG_PROJECT_ROOT / "src" / "config.py").is_file()


def test_default_paths_resolve_under_project_root() -> None:
    settings = SETTINGS
    assert settings.data_dir == PROJECT_ROOT / "data"
    assert settings.log_dir == PROJECT_ROOT / "data" / "logs"
    assert settings.employees_dir == PROJECT_ROOT / "data" / "employees"
    assert settings.face_samples_dir == PROJECT_ROOT / "data" / "face_samples"
    assert settings.embeddings_dir == PROJECT_ROOT / "data" / "embeddings"
    assert settings.models_dir == PROJECT_ROOT / "models"
    assert settings.reports_dir == PROJECT_ROOT / "reports"


def test_paths_are_cwd_independent(tmp_path: Path) -> None:
    """Importing config from a foreign cwd must still resolve repo paths."""
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "from src.config import PROJECT_ROOT, SETTINGS;"
        "assert str(PROJECT_ROOT) == r'" + str(PROJECT_ROOT) + "';"
        "assert str(SETTINGS.data_dir).endswith('data');"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


# ---------------------------------------------------------------------------
# 3. Expected values are available and immutable
# ---------------------------------------------------------------------------


def test_camera_constants() -> None:
    assert CAMERA.index == 0
    assert CAMERA.frame_width == 1280
    assert CAMERA.frame_height == 720
    assert CAMERA.target_fps == 30


def test_face_processing_constants() -> None:
    fp = FACE_PROCESSING
    assert fp.min_face_size_px == 80
    assert 0 < fp.detection_confidence <= 1
    assert 0 < fp.recognition_threshold < 1
    assert fp.min_brightness < fp.max_brightness <= 255
    assert fp.min_sharpness_score >= 0


def test_enrollment_constants_ordered() -> None:
    assert ENROLLMENT.min_samples == 3
    assert ENROLLMENT.target_samples == 5
    assert ENROLLMENT.max_samples == 10
    assert ENROLLMENT.min_samples <= ENROLLMENT.target_samples <= ENROLLMENT.max_samples


def test_constants_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        CAMERA.index = 5  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        FACE_PROCESSING.recognition_threshold = 0.9  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ENROLLMENT.max_samples = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        SETTINGS.debug = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 4. Environment overrides work where intentionally supported
# ---------------------------------------------------------------------------


def test_env_overrides_app_env_and_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG", "1")
    settings = load_settings()
    assert settings.app_env == "production"
    assert settings.debug is True


def test_env_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("APP_ENV", "DEBUG", "DATA_DIR", "LOG_DIR"):
        monkeypatch.delenv(var, raising=False)
    settings = load_settings()
    assert settings.app_env == "development"
    assert settings.debug is False
    assert settings.data_dir == PROJECT_ROOT / "data"
    assert settings.log_dir == PROJECT_ROOT / "data" / "logs"


def test_env_overrides_paths_relative_and_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_DIR", "custom-data")
    monkeypatch.setenv("LOG_DIR", str(PROJECT_ROOT.parent / "abs-logs"))
    settings = load_settings()
    assert settings.data_dir == PROJECT_ROOT / "custom-data"
    assert settings.employees_dir == PROJECT_ROOT / "custom-data" / "employees"
    assert settings.log_dir.is_absolute()


# ---------------------------------------------------------------------------
# 5. Invalid configuration values are handled appropriately
# ---------------------------------------------------------------------------


def test_invalid_app_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "not-an-env")
    with pytest.raises(ConfigError):
        load_settings()


def test_invalid_debug_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG", "maybe")
    with pytest.raises(ConfigError):
        load_settings()


def test_config_error_is_value_error() -> None:
    assert issubclass(ConfigError, ValueError)


def test_invalid_constant_values_raise_value_error() -> None:
    with pytest.raises(ValueError):
        CameraConfig(index=-1)
    with pytest.raises(ValueError):
        CameraConfig(target_fps=0)
    with pytest.raises(ValueError):
        FaceProcessingConfig(recognition_threshold=1.5)
    with pytest.raises(ValueError):
        FaceProcessingConfig(min_brightness=200, max_brightness=100)
    with pytest.raises(ValueError):
        EnrollmentConfig(min_samples=6, target_samples=5, max_samples=10)
    with pytest.raises(ValueError):
        EnrollmentConfig(min_samples=0)


# ---------------------------------------------------------------------------
# 6. No side effects on import / load
# ---------------------------------------------------------------------------


def test_load_settings_creates_no_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_settings must not touch the filesystem."""
    fresh = tmp_path / "nowhere"
    monkeypatch.setenv("DATA_DIR", str(fresh / "data"))
    settings = load_settings()
    assert settings.data_dir == fresh / "data"
    assert not fresh.exists(), "load_settings created directories (side effect!)"


def test_ensure_directories_creates_expected_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "svc"
    monkeypatch.setenv("DATA_DIR", str(base / "data"))
    monkeypatch.setenv("LOG_DIR", str(base / "logs"))
    settings = load_settings()
    ensure_directories(settings)
    assert (base / "data" / "employees").is_dir()
    assert (base / "data" / "face_samples").is_dir()
    assert (base / "data" / "embeddings").is_dir()
    assert (base / "logs").is_dir()
    assert settings.models_dir.is_dir()
    assert settings.reports_dir.is_dir()


def test_config_import_does_not_pull_cv_stack() -> None:
    """Importing src.config in a fresh interpreter must not import cv2 or
    any ML library — no device, model, or heavy stack may be touched."""
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.config;"
        "banned = {'cv2', 'mediapipe', 'insightface', 'onnxruntime'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
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
# 5b. P2-6 S1: check-out cooldown constant (configuration only)
# ---------------------------------------------------------------------------


def test_attendance_check_out_cooldown_defaults_mirror_check_in() -> None:
    """The P2-6 field mirrors the check-in constant by default, and the
    existing check-in default is untouched (300.0 stays 300.0)."""
    assert ATTENDANCE.check_in_cooldown_seconds == 300.0
    assert ATTENDANCE.check_out_cooldown_seconds == 300.0
    assert AttendanceConfig().check_in_cooldown_seconds == 300.0
    assert AttendanceConfig().check_out_cooldown_seconds == 300.0


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"check_out_cooldown_seconds": 0},
        {"check_out_cooldown_seconds": -5},
        {"check_out_cooldown_seconds": -0.5},
        {"check_out_cooldown_seconds": float("inf")},
        {"check_out_cooldown_seconds": float("nan")},
        {"check_out_cooldown_seconds": True},   # bool rejected
        {"check_out_cooldown_seconds": "300"},  # str rejected
        {"check_out_cooldown_seconds": None},
    ],
)
def test_attendance_check_out_cooldown_invalid_values_rejected(
    bad_kwargs: dict,
) -> None:
    """Validation mirrors the check-in field exactly: booleans, strings,
    non-finite values, zero, and negatives are rejected at construction."""
    with pytest.raises(ValueError):
        AttendanceConfig(**bad_kwargs)


def test_attendance_check_out_cooldown_accepts_positive_values() -> None:
    """Real finite positive numbers are accepted, ints included."""
    cfg = AttendanceConfig(check_out_cooldown_seconds=0.5)
    assert cfg.check_out_cooldown_seconds == 0.5
    cfg_int = AttendanceConfig(check_out_cooldown_seconds=60)
    assert cfg_int.check_out_cooldown_seconds == 60


def test_attendance_frozen_config_instance_still_frozen() -> None:
    """The new field does not weaken the frozen-constant convention."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        ATTENDANCE.check_out_cooldown_seconds = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 7. Phase 3 (Gate 1.2): database URLs, loopback-only API_HOST, strict API_PORT
# ---------------------------------------------------------------------------


def test_default_phase3_settings_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset Phase 3 variables resolve to the documented safe defaults:
    no DSN targets (``None`` — never a silent local fall-through),
    loopback API host, port 8000."""
    for var in (
        "ATTENDANCE_DATABASE_URL",
        "ATTENDANCE_TEST_DATABASE_URL",
        "API_HOST",
        "API_PORT",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = load_settings()
    assert settings.database_url is None
    assert settings.test_database_url is None
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("ATTENDANCE_DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/attendance"),
        ("ATTENDANCE_TEST_DATABASE_URL", "postgresql://u:p@127.0.0.1:5433/attendance_test"),
    ],
)
def test_env_sets_database_urls_verbatim(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    """DSNs pass through verbatim (no syntax validation in the config
    layer — psycopg owns the connection-string dialect at first use)."""
    monkeypatch.setenv(var, value)
    settings = load_settings()
    assert getattr(settings, "database_url" if var == "ATTENDANCE_DATABASE_URL" else "test_database_url") == value


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_database_url_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ATTENDANCE_DATABASE_URL", value)
    settings = load_settings()
    assert settings.database_url is None


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "127.255.0.7", "::1"],
)
def test_api_host_accepts_loopback_values(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("API_HOST", host)
    assert load_settings().api_host == host


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",        # all interfaces — explicitly forbidden
        "192.168.1.10",   # LAN address — forbidden
        "example.com",    # bare hostname — forbidden
        "not-an-address",
        "  ",             # blank
        "",               # empty
    ],
)
def test_api_host_rejects_non_loopback_and_invalid_values(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("API_HOST", host)
    with pytest.raises(ConfigError):
        load_settings()


@pytest.mark.parametrize(
    "port",
    ["1", "80", "8000", "65535"],
)
def test_api_port_accepts_valid_range(
    monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    monkeypatch.setenv("API_PORT", port)
    assert load_settings().api_port == int(port)


@pytest.mark.parametrize(
    "port",
    [
        "0",        # below range
        "65536",     # above range
        "-1",        # negative — sign rejected before range check
        "8000.5",    # decimal separator rejected
        "＋8000",    # non-ASCII digit run rejected
        "８０００",    # full-width digits rejected
        "eight",     # not a number at all
        "",          # empty string
        "  ",        # whitespace-only
    ],
)
def test_api_port_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    monkeypatch.setenv("API_PORT", port)
    with pytest.raises(ConfigError):
        load_settings()


def test_direct_construction_validates_phase3_fields_too() -> None:
    """``__post_init__`` validates identically for direct construction (same
    rule as every other config dataclass here: invalid values never survive)."""
    with pytest.raises(ConfigError):
        AppSettings(api_host="0.0.0.0")
    with pytest.raises(ConfigError):
        AppSettings(api_port=0)
    with pytest.raises(ConfigError):
        AppSettings(api_port=65536)
    with pytest.raises(ConfigError):
        AppSettings(api_port=True)  # bool rejected like every other int field


def test_phase3_fields_frozen_like_the_rest() -> None:
    settings = load_settings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.api_host = "0.0.0.0"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.api_port = 9999  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.database_url = "postgresql://attacker@127.0.0.1/evil"  # type: ignore[misc]
