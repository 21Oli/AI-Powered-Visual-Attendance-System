"""Centralized configuration and constants for the AI-Powered Visual
Attendance System (Phase 1 — Step 02).

This module is the single source of truth for:

* application constants (frozen dataclasses — never stored in ``.env``),
* environment-specific settings (loaded from environment variables),
* project filesystem paths (``pathlib.Path``, working-directory independent).

Design rules
------------
* **No side effects on import.** Importing this module opens no devices,
  loads no models, and creates no directories. Use
  :func:`ensure_directories` explicitly when directory creation is wanted.
* **No global mutable state.** Every configuration object is a frozen
  dataclass; mutation attempts raise :class:`dataclasses.FrozenInstanceError`.
* **Environment variables are for environment-specific values only**
  (environment name, debug flag, externally configurable paths).
  Application constants (camera defaults, face-processing thresholds,
  enrollment sample counts) live *here*, not in ``.env``.
* **Fail fast.** Invalid constant definitions raise ``ValueError`` at
  import time; invalid environment values raise :class:`ConfigError`.
* **Independent from business logic.** This module must never import
  other ``src`` modules or perform domain work.

Future steps tune thresholds *only here* instead of scattering magic
numbers through the pipeline modules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from dotenv import load_dotenv

__all__ = [
    "APP_NAME",
    "PROJECT_ROOT",
    "CameraConfig",
    "FaceProcessingConfig",
    "EnrollmentConfig",
    "CAMERA",
    "FACE_PROCESSING",
    "ENROLLMENT",
    "AppSettings",
    "Settings",
    "ConfigError",
    "load_settings",
    "ensure_directories",
    "SETTINGS",
]

# ---------------------------------------------------------------------------
# Application identity & project root (constants)
# ---------------------------------------------------------------------------

APP_NAME: Final[str] = "AI-Powered Visual Attendance System"

#: Repository root, derived from this file's location:
#: ``<root>/src/config.py`` -> ``<root>``. Constructed from ``__file__`` so
#: paths stay correct regardless of the current working directory.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """Raised when an environment-provided configuration value is invalid."""


def _strict_bool(raw: str) -> bool:
    """Parse a boolean from an environment string (strict, fail fast)."""
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(
        f"Invalid boolean value {raw!r}: expected one of "
        "1/true/yes/on or 0/false/no/off"
    )


# ---------------------------------------------------------------------------
# Camera constants (configuration only — Step 04 implements the service)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CameraConfig:
    """Constants for the future live camera service (Step 04)."""

    #: Default device index for ``cv2.VideoCapture``.
    index: int = 0
    frame_width: int = 1280
    frame_height: int = 720
    #: Preferred capture frame rate; advisory only — the device decides.
    target_fps: int = 30

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError(f"camera index must be >= 0, got {self.index}")
        if self.frame_width <= 0 or self.frame_height <= 0:
            raise ValueError(
                "frame dimensions must be positive, got "
                f"{self.frame_width}x{self.frame_height}"
            )
        if self.target_fps <= 0:
            raise ValueError(f"target_fps must be positive, got {self.target_fps}")


# ---------------------------------------------------------------------------
# Face-processing constants (Step 05/06/08/10 tune thresholds here)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FaceProcessingConfig:
    """Constants for detection, quality, and recognition (future steps)."""

    #: Smallest face box side (px) worth processing at pipeline quality.
    min_face_size_px: int = 80
    #: Detector confidence floor (0, 1].
    detection_confidence: float = 0.5
    #: Cosine-similarity decision threshold for face recognition.
    recognition_threshold: float = 0.6
    #: Laplacian-variance sharpness floor for quality gating.
    min_sharpness_score: float = 50.0
    #: Mean-brightness acceptance window for quality gating.
    min_brightness: float = 40.0
    max_brightness: float = 220.0

    def __post_init__(self) -> None:
        if self.min_face_size_px <= 0:
            raise ValueError(
                f"min_face_size_px must be positive, got {self.min_face_size_px}"
            )
        if not 0.0 < self.detection_confidence <= 1.0:
            raise ValueError(
                "detection_confidence must be in (0, 1], got "
                f"{self.detection_confidence}"
            )
        if not 0.0 < self.recognition_threshold < 1.0:
            raise ValueError(
                "recognition_threshold must be in (0, 1), got "
                f"{self.recognition_threshold}"
            )
        if self.min_sharpness_score < 0:
            raise ValueError(
                f"min_sharpness_score must be >= 0, got {self.min_sharpness_score}"
            )
        if not 0 <= self.min_brightness < self.max_brightness <= 255:
            raise ValueError(
                "brightness window must satisfy 0 <= min < max <= 255, got "
                f"[{self.min_brightness}, {self.max_brightness}]"
            )


# ---------------------------------------------------------------------------
# Enrollment constants (configuration only — Step 07 implements enrollment)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrollmentConfig:
    """Constants for future multi-image enrollment (Step 07)."""

    #: Below this many accepted samples an enrollment is rejected.
    min_samples: int = 3
    #: Guidance target for a good enrollment.
    target_samples: int = 5
    #: Hard cap on stored samples per employee.
    max_samples: int = 10

    def __post_init__(self) -> None:
        if self.min_samples < 1:
            raise ValueError(f"min_samples must be >= 1, got {self.min_samples}")
        if not self.min_samples <= self.target_samples <= self.max_samples:
            raise ValueError(
                "sample counts must satisfy min <= target <= max, got "
                f"min={self.min_samples}, target={self.target_samples}, "
                f"max={self.max_samples}"
            )


#: Frozen constant instances — the project-wide single source of truth.
CAMERA: Final[CameraConfig] = CameraConfig()
FACE_PROCESSING: Final[FaceProcessingConfig] = FaceProcessingConfig()
ENROLLMENT: Final[EnrollmentConfig] = EnrollmentConfig()


# ---------------------------------------------------------------------------
# Environment-specific settings (the only values driven by environment)
# ---------------------------------------------------------------------------

AppEnv = Literal["development", "testing", "staging", "production"]

_VALID_ENVS: Final[tuple[str, ...]] = (
    "development",
    "testing",
    "staging",
    "production",
)


def _resolve_env_app_env() -> AppEnv:
    raw = os.getenv("APP_ENV", "development").strip().lower()
    if raw not in _VALID_ENVS:
        raise ConfigError(
            f"Invalid APP_ENV {raw!r}: expected one of {', '.join(_VALID_ENVS)}"
        )
    return raw  # type: ignore[return-value]


def _resolve_env_debug() -> bool:
    return _strict_bool(os.getenv("DEBUG", "false"))


def _resolve_env_path(var: str, default: str) -> Path:
    """Resolve a path env var; relative values anchor to ``PROJECT_ROOT``."""
    raw = os.getenv(var, default)
    path = Path(raw)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


@dataclass(frozen=True)
class AppSettings:
    """Environment-specific settings plus derived project paths.

    Construct via :func:`load_settings` (reads the environment) or use the
    module-level :data:`SETTINGS` default instance.
    """

    app_env: AppEnv = "development"
    debug: bool = False
    #: Base directory for runtime data (relative to PROJECT_ROOT unless absolute).
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    #: Log directory consumed by ``src/logging_setup.py`` (same anchoring rule).
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "logs")

    # -- Derived paths (constants by composition, not environment) ---------

    @property
    def employees_dir(self) -> Path:
        return self.data_dir / "employees"

    @property
    def face_samples_dir(self) -> Path:
        return self.data_dir / "face_samples"

    @property
    def embeddings_dir(self) -> Path:
        return self.data_dir / "embeddings"

    @property
    def models_dir(self) -> Path:
        return PROJECT_ROOT / "models"

    @property
    def reports_dir(self) -> Path:
        return PROJECT_ROOT / "reports"


def load_settings() -> AppSettings:
    """Build an :class:`AppSettings` from the environment.

    Loads ``.env`` first (existing process environment always wins), then
    validates every value — invalid values raise :class:`ConfigError`
    instead of failing silently later.
    """
    # Non-overriding: real environment variables take precedence over .env.
    load_dotenv(override=False)
    return AppSettings(
        app_env=_resolve_env_app_env(),
        debug=_resolve_env_debug(),
        data_dir=_resolve_env_path("DATA_DIR", "data"),
        log_dir=_resolve_env_path("LOG_DIR", "data/logs"),
    )


def ensure_directories(settings: AppSettings) -> None:
    """Create the directories referenced by ``settings`` (explicit call only).

    Import-time code must never call this — directory creation is a side
    effect that belongs to application startup, not module import.
    """
    for directory in (
        settings.data_dir,
        settings.log_dir,
        settings.employees_dir,
        settings.face_samples_dir,
        settings.embeddings_dir,
        settings.models_dir,
        settings.reports_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


#: Default settings instance for simple imports. Frozen; no side effects.
SETTINGS: Final[AppSettings] = load_settings()
