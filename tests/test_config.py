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
    CAMERA,
    ENROLLMENT,
    FACE_PROCESSING,
    SETTINGS,
    AppSettings,
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
