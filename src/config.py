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

import ipaddress
import math
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
    "QualityConfig",
    "EnrollmentConfig",
    "RecognitionConfig",
    "TemporalVerificationConfig",
    "UnknownFaceConfig",
    "AttendanceConfig",
    "CAMERA",
    "FACE_PROCESSING",
    "QUALITY",
    "ENROLLMENT",
    "RECOGNITION",
    "TEMPORAL_VERIFICATION",
    "UNKNOWN_FACE",
    "ATTENDANCE",
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
    #: Hard cap on frame attempts per enrollment session (poor-quality
    #: frames, unreadable frames, and no-face frames all count) — the
    #: bound that prevents endless capture loops (Step 07).
    max_capture_attempts: int = 60

    def __post_init__(self) -> None:
        if self.min_samples < 1:
            raise ValueError(f"min_samples must be >= 1, got {self.min_samples}")
        if not self.min_samples <= self.target_samples <= self.max_samples:
            raise ValueError(
                "sample counts must satisfy min <= target <= max, got "
                f"min={self.min_samples}, target={self.target_samples}, "
                f"max={self.max_samples}"
            )
        if self.max_capture_attempts < self.min_samples:
            raise ValueError(
                "max_capture_attempts must be >= min_samples, got "
                f"{self.max_capture_attempts} < {self.min_samples}"
            )


# ---------------------------------------------------------------------------
# Quality constants (configuration only — Step 06 implements the assessor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityConfig:
    """Thresholds for Step 06 face-quality gating.

    Defaults are initial, testable calibration starting points — **not**
    claims of production biometric accuracy. Phase 2 evaluation tunes
    them here (single module, per ARCHITECTURE.md D5).
    """

    #: Smallest usable (frame-clamped) box side (px) accepted per axis.
    min_face_size_px: int = 80
    #: Laplacian-variance sharpness floor on the grayscale face crop.
    min_sharpness: float = 50.0
    #: Mean-brightness acceptance window (grayscale 0–255).
    min_brightness: float = 40.0
    max_brightness: float = 220.0
    #: Landmark orientation proxy tolerances (normalized offsets).
    pose_yaw_tolerance: float = 0.15
    #: Vertical offset allowance is looser — yaw (side-to-side) is the
    #: stronger enrollment-quality signal for a face-looking-at-camera
    #: check; pitch still rejects extreme up/down tilts.
    pose_pitch_tolerance: float = 0.35
    #: "optional": skip the pose check without landmarks (metrics None).
    #: "required": reject with NO_LANDMARKS when landmarks are absent.
    pose_policy: Literal["optional", "required"] = "optional"
    #: Frame must contain exactly one face for enrollment-quality use.
    require_single_face: bool = True

    def __post_init__(self) -> None:
        if self.min_face_size_px <= 0:
            raise ValueError(
                f"min_face_size_px must be positive, got {self.min_face_size_px}"
            )
        if self.min_sharpness < 0:
            raise ValueError(
                f"min_sharpness must be >= 0, got {self.min_sharpness}"
            )
        if not 0 <= self.min_brightness < self.max_brightness <= 255:
            raise ValueError(
                "brightness window must satisfy 0 <= min < max <= 255, got "
                f"[{self.min_brightness}, {self.max_brightness}]"
            )
        if self.pose_yaw_tolerance < 0 or self.pose_pitch_tolerance < 0:
            raise ValueError(
                "pose tolerances must be >= 0, got yaw="
                f"{self.pose_yaw_tolerance}, pitch={self.pose_pitch_tolerance}"
            )
        if self.pose_policy not in ("optional", "required"):
            raise ValueError(
                f"pose_policy must be 'optional' or 'required', got "
                f"{self.pose_policy!r}"
            )


# ---------------------------------------------------------------------------
# Recognition constants (configuration only — Step 10 implements recognition)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecognitionConfig:
    """Constants for future face recognition (Step 10).

    Defaults are **provisional, testable starting points** — not claims
    of production biometric accuracy. The literature SFace workflow
    matches *normalized* features; Step 08 (crop-only mode, no landmark
    alignment) produces **raw** embeddings, so the raw-feature cosine
    scale differs from commonly quoted thresholds. Calibration against
    representative positive/negative footage is Phase 2 work, tuned
    exactly here (per ARCHITECTURE.md D5).
    """

    #: Minimum cosine similarity for accepting a best candidate
    #: (raw-feature cosine scale). Provisional until evaluated.
    min_similarity: float = 0.36
    #: Minimum margin between the best and runner-up scores for an
    #: unambiguous match; ``None`` disables the ambiguity check (any
    #: margin above this value is accepted). Provisional until evaluated.
    min_margin: float | None = None
    #: Only recognize candidates whose published enrollment manifest
    #: (Step 07 ``employee.json``) exists — the ``"enrollment exists"``
    #: marker. Active-status gating is handled separately by the
    #: recognition service's eligibility logic.
    require_enrollment: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.min_similarity, (int, float)) or isinstance(
            self.min_similarity, bool
        ):
            raise ValueError(
                "min_similarity must be a real number, got "
                f"{type(self.min_similarity).__name__}"
            )
        if not 0.0 <= self.min_similarity <= 1.0:
            raise ValueError(
                "min_similarity must be a finite cosine-similarity value "
                f"in [0, 1], got {self.min_similarity}"
            )
        if self.min_margin is not None:
            if not isinstance(self.min_margin, (int, float)) or isinstance(
                self.min_margin, bool
            ):
                raise ValueError(
                    "min_margin must be None or a real number, got "
                    f"{type(self.min_margin).__name__}"
                )
            if not 0.0 <= self.min_margin <= 2.0:
                raise ValueError(
                    "min_margin must be in [0, 2] on the raw cosine scale, "
                    f"got {self.min_margin}"
                )
        if not isinstance(self.require_enrollment, bool):
            raise ValueError(
                "require_enrollment must be a bool, got "
                f"{type(self.require_enrollment).__name__}"
            )


# ---------------------------------------------------------------------------
# Temporal-verification constants (configuration only — Step 11 implements it)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemporalVerificationConfig:
    """Constants for temporal (multi-frame) identity verification (Step 11).

    Defaults are **initial engineering starting points**, explicitly NOT
    calibrated production values — they must be tuned with real camera
    footage in Phase 2 (tuned exactly here, per ARCHITECTURE.md D5).

    Policy (conservative, documented): only ``MATCHED`` recognition
    frames for the *same* employee ID accumulate positive evidence;
    ANY non-matched frame (``UNKNOWN`` / ``AMBIGUOUS`` /
    ``NO_CANDIDATES``) or a different matched ID breaks the streak.
    """

    #: Number of consecutive consistent ``MATCHED`` frames (same
    #: employee ID, uninterrupted) required before the identity becomes
    #: temporally verified. Provisional until calibrated.
    min_consistent_frames: int = 3
    #: Maximum number of recent recognition evidence frames retained
    #: by the verifier (bounded state — never unbounded history).
    #: Must be >= ``min_consistent_frames`` so verification is possible.
    window_frames: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.min_consistent_frames, int) or isinstance(
            self.min_consistent_frames, bool
        ):
            raise ValueError(
                "min_consistent_frames must be a plain int (bool and "
                "float rejected), got "+ f"{type(self.min_consistent_frames).__name__}"
            )
        if self.min_consistent_frames < 1:
            raise ValueError(
                "min_consistent_frames must be >= 1, got "
                f"{self.min_consistent_frames}"
            )
        if not isinstance(self.window_frames, int) or isinstance(
            self.window_frames, bool
        ):
            raise ValueError(
                "window_frames must be a plain int (bool and float "
                f"rejected), got {type(self.window_frames).__name__}"
            )
        if self.window_frames < 1:
            raise ValueError(
                f"window_frames must be >= 1, got {self.window_frames}"
            )
        if self.window_frames < self.min_consistent_frames:
            raise ValueError(
                "window_frames must be >= min_consistent_frames "
                "(otherwise verification is impossible), got "
                f"window_frames={self.window_frames} < "
                f"min_consistent_frames={self.min_consistent_frames}"
            )


# ---------------------------------------------------------------------------
# Unknown-face-handling constants (configuration only — Step 12 implements it)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnknownFaceConfig:
    """Constants for unknown-face handling (Step 12).

    Defaults are **initial engineering defaults**, explicitly NOT
    calibrated operational or security guarantees — tuned with real
    camera data in Phase 2 (tuned exactly here, per ARCHITECTURE.md
    D5). Conservative semantics: an unresolved episode suppresses
    repeated handling for ``episode_cooldown_seconds`` from the alert
    instant (non-refreshing — deterministic expiry); state is bounded
    per caller-managed session (LRU eviction beyond ``max_sessions``).
    """

    #: An unresolved episode starts with one observed ("alert") frame
    #: and suppresses further handling for this many seconds from the
    #: alert instant. Non-refreshing: the episode expires
    #: deterministically, even if unresolved frames keep arriving.
    episode_cooldown_seconds: float = 30.0
    #: Maximum number of caller-managed sessions whose episode state is
    #: retained. Beyond this, the least-recently-used session's state is
    #: evicted (bounded memory; worst case one extra alert per eviction).
    max_sessions: int = 8
    #: Maximum accepted length (characters) of a caller-provided session
    #: key. Longer keys are rejected rather than truncated.
    max_session_key_length: int = 128

    def __post_init__(self) -> None:
        if (
            not isinstance(self.episode_cooldown_seconds, (int, float))
            or isinstance(self.episode_cooldown_seconds, bool)
        ):
            raise ValueError(
                "episode_cooldown_seconds must be a real number (bool "
                "rejected), got "
                f"{type(self.episode_cooldown_seconds).__name__}"
            )
        cooldown = float(self.episode_cooldown_seconds)
        if not math.isfinite(cooldown) or cooldown <= 0.0:
            raise ValueError(
                "episode_cooldown_seconds must be a finite positive "
                "number of seconds, got "
                f"{self.episode_cooldown_seconds}"
            )
        if not isinstance(self.max_sessions, int) or isinstance(
            self.max_sessions, bool
        ):
            raise ValueError(
                "max_sessions must be a plain int (bool and float "
                f"rejected), got {type(self.max_sessions).__name__}"
            )
        if self.max_sessions < 1:
            raise ValueError(
                f"max_sessions must be >= 1, got {self.max_sessions}"
            )
        if not isinstance(self.max_session_key_length, int) or isinstance(
            self.max_session_key_length, bool
        ):
            raise ValueError(
                "max_session_key_length must be a plain int (bool and "
                "float rejected), got "
                f"{type(self.max_session_key_length).__name__}"
            )
        if self.max_session_key_length < 1:
            raise ValueError(
                "max_session_key_length must be >= 1, got "
                f"{self.max_session_key_length}"
            )
CAMERA: Final[CameraConfig] = CameraConfig()
FACE_PROCESSING: Final[FaceProcessingConfig] = FaceProcessingConfig()
QUALITY: Final[QualityConfig] = QualityConfig()
ENROLLMENT: Final[EnrollmentConfig] = EnrollmentConfig()
RECOGNITION: Final[RecognitionConfig] = RecognitionConfig()
TEMPORAL_VERIFICATION: Final[TemporalVerificationConfig] = (
    TemporalVerificationConfig()
)
# ---------------------------------------------------------------------------
# Attendance-event constants (configuration only — Step 13 implements it)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttendanceConfig:
    """Constants for the attendance-event engine (Step 13).

    Defaults are **initial engineering values**, explicitly NOT
    calibrated workplace attendance policies — real policies (shift
    rules, daily dedup, grace periods) are Phase 2 business work and
    must be tuned with real deployment data (tuned exactly here, per
    ARCHITECTURE.md D5).

    Duplicate-suppression semantics (conservative, documented):
    exactly one ``CHECK_IN`` event per employee per
    ``check_in_cooldown_seconds`` of engine-lifetime state; repeated
    verified frames do NOT refresh the cooldown anchor. State is
    bounded per unique verified employee (LRU eviction beyond
    ``max_tracked_employees``). Durable/cross-process storage is out
    of scope for Step 13 (see the engine module docstring).
    """

    #: Duplicate-suppression interval in seconds: repeated temporally
    #: verified results for the same employee within this many seconds
    #: of that employee's last accepted event suppress the decision.
    #: Boundary is exact and non-refreshing (engine module docstring).
    check_in_cooldown_seconds: float = 300.0
    #: Duplicate-suppression interval in seconds for CHECK_OUT events
    #: (P2-6, Step S1): mirrors ``check_in_cooldown_seconds`` as an
    #: initial engineering value, explicitly NOT a calibrated workplace
    #: policy — same epistemic status as the check-in constant. Validated
    #: exactly like the check-in field; consumed by the attendance engine
    #: in a later P2-6 step (nothing in the current engine code reads it
    #: yet), so the existing check-in semantics are untouched.
    check_out_cooldown_seconds: float = 300.0
    #: Maximum number of employees whose per-employee suppression
    #: anchors are retained. Beyond this, the least-recently-seen
    #: employee's anchor is evicted (bounded memory; worst case after
    #: eviction is one additional check-in event). Sized for typical
    #: site employee counts; oversized deployments must raise it.
    max_tracked_employees: int = 64

    def __post_init__(self) -> None:
        if (
            not isinstance(self.check_in_cooldown_seconds, (int, float))
            or isinstance(self.check_in_cooldown_seconds, bool)
        ):
            raise ValueError(
                "check_in_cooldown_seconds must be a real number (bool "
                "rejected), got "
                f"{type(self.check_in_cooldown_seconds).__name__}"
            )
        cooldown = float(self.check_in_cooldown_seconds)
        if not math.isfinite(cooldown) or cooldown <= 0.0:
            raise ValueError(
                "check_in_cooldown_seconds must be a finite positive "
                f"number of seconds, got {self.check_in_cooldown_seconds}"
            )
        if (
            not isinstance(self.check_out_cooldown_seconds, (int, float))
            or isinstance(self.check_out_cooldown_seconds, bool)
        ):
            raise ValueError(
                "check_out_cooldown_seconds must be a real number (bool "
                "rejected), got "
                f"{type(self.check_out_cooldown_seconds).__name__}"
            )
        check_out_cooldown = float(self.check_out_cooldown_seconds)
        if (
            not math.isfinite(check_out_cooldown)
            or check_out_cooldown <= 0.0
        ):
            raise ValueError(
                "check_out_cooldown_seconds must be a finite positive "
                "number of seconds, got "
                f"{self.check_out_cooldown_seconds}"
            )
        if not isinstance(self.max_tracked_employees, int) or isinstance(
            self.max_tracked_employees, bool
        ):
            raise ValueError(
                "max_tracked_employees must be a plain int (bool and "
                "float rejected), got "
                f"{type(self.max_tracked_employees).__name__}"
            )
        if self.max_tracked_employees < 1:
            raise ValueError(
                "max_tracked_employees must be >= 1, got "
                f"{self.max_tracked_employees}"
            )


UNKNOWN_FACE: Final[UnknownFaceConfig] = UnknownFaceConfig()
ATTENDANCE: Final[AttendanceConfig] = AttendanceConfig()


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


def _resolve_env_database_url(var: str) -> str | None:
    """Resolve a DSN env var; unset and whitespace-only both mean unset.

    No DSN *syntax* validation happens here — the DSN value is passed
    through verbatim (only surrounding whitespace is trimmed, which
    cannot corrupt a psycopg-legal connection string) and its syntax is
    psycopg's contract, checked at first use in :mod:`src.db`. Keeping
    this module driver-free preserves the lightweight import rule.
    """
    raw = os.getenv(var, "").strip()
    return raw if raw else None


def _resolve_env_api_host() -> str:
    """Resolve ``API_HOST`` (loopback-only; failed values raise ConfigError)."""
    return os.getenv("API_HOST", "127.0.0.1").strip()


def _resolve_env_api_port() -> int:
    """Resolve ``API_PORT`` as a strict ASCII integer in ``[1, 65535]``.

    ``str.isdigit`` alone accepts non-ASCII digit runs (e.g. Arabic-Indic
    digits), so the value must additionally be ASCII — signs and decimal
    separators are rejected the same way.
    """
    raw = os.getenv("API_PORT", "8000").strip()
    if not (raw.isascii() and raw.isdigit()):
        raise ConfigError(
            f"Invalid API_PORT {raw!r}: expected an ASCII integer port number"
        )
    return _validate_api_port_value(int(raw))


def _validate_api_port_value(port: int) -> int:
    """Validate a concrete port value; shared by env and direct construction."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigError(f"api_port must be an int, got {type(port).__name__}")
    if not 1 <= port <= 65535:
        raise ConfigError(
            f"api_port {port} is outside the valid range [1, 65535]"
        )
    return port


def _validate_api_host_value(host: str) -> str:
    """Validate a concrete host value (loopback-only); shared by both paths.

    Accepts ``localhost``, any ``127.0.0.0/8`` address, and ``::1``.
    Bare hostnames and non-loopback addresses are rejected: Phase 3's API
    is loopback-only and must never accidentally bind outward.
    """
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("api_host must be a non-empty hostname or IP address")
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(
            f"Invalid API_HOST {host!r}: not a recognized IP address "
            "(use localhost, a 127.0.0.0/8 address, or ::1)"
        ) from None
    if not address.is_loopback:
        raise ConfigError(
            f"Invalid API_HOST {host!r}: non-loopback binding is forbidden "
            "in Phase 3 (use 127.0.0.0/8, ::1, or localhost)"
        )
    return host


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
    #: PostgreSQL DSN for employee metadata (Phase 3). ``None`` means unset:
    #: all database-touching layers must fail fast, never fall through to a
    #: local default connection target.
    database_url: str | None = None
    #: Dedicated integration-test DSN (Phase 3 test gates only). Never set
    #: this to a shared or production database.
    test_database_url: str | None = None
    #: Development API bind address. Loopback-only, enforced below; the
    #: Phase 3 API is explicitly not network-exposed.
    api_host: str = "127.0.0.1"
    #: Development API bind port. Validated to a strict ``[1, 65535]`` range.
    api_port: int = 8000

    def __post_init__(self) -> None:
        # Same rule as every other configuration object here: an invalid
        # value must fail at construction, not silently survive until it is
        # read later. The loopback constraint is this dataclass's own
        # constant-level rule (like CameraConfig index/frame bounds).
        _validate_api_port_value(self.api_port)
        _validate_api_host_value(self.api_host)

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
    def attendance_dir(self) -> Path:
        return self.data_dir / "attendance"

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
        database_url=_resolve_env_database_url("ATTENDANCE_DATABASE_URL"),
        test_database_url=_resolve_env_database_url(
            "ATTENDANCE_TEST_DATABASE_URL"
        ),
        api_host=_resolve_env_api_host(),
        api_port=_resolve_env_api_port(),
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
