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
from datetime import timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

import src.config as config_module
from src.config import (
    APP_NAME,
    ATTENDANCE,
    CAMERA,
    ENROLLMENT,
    FACE_PROCESSING,
    LOGIN_THROTTLE,
    PASSWORD_HASHING,
    SECURE_COOKIE_ENVIRONMENTS,
    SESSION_COOKIE,
    SESSION_POLICY,
    SETTINGS,
    AppSettings,
    AttendanceConfig,
    CameraConfig,
    ConfigError,
    EnrollmentConfig,
    FaceProcessingConfig,
    LoginThrottleConfig,
    SessionCookieConfig,
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
    assert settings.exports_dir == PROJECT_ROOT / "data" / "exports"
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


@pytest.fixture(autouse=True)
def _isolate_from_project_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every config test hermetic with respect to the developer's .env.

    ``load_settings()`` calls ``load_dotenv(override=False)``, so a real
    process environment always wins, but a local ``.env`` file still fills
    in anything this module deletes or leaves unset. These tests describe
    the environment -> ``AppSettings`` mapping, not the developer's machine
    setup, so the dotenv read is suppressed here and nowhere else; the
    production contract (environment wins, ``.env`` is a convenience) is
    untouched.
    """
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)


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


# ---------------------------------------------------------------------------
# 8. Phase 5 P5.4.2: authentication API configuration
# ---------------------------------------------------------------------------


def test_session_policy_values_are_the_approved_ones() -> None:
    assert SESSION_POLICY.idle_timeout == timedelta(hours=8)
    assert SESSION_POLICY.absolute_lifetime == timedelta(hours=12)


def test_session_cookie_attributes_match_the_approved_decision() -> None:
    assert SESSION_COOKIE.name == "va_session"
    assert SESSION_COOKIE.path == "/api/v1"
    assert SESSION_COOKIE.http_only is True
    assert SESSION_COOKIE.same_site == "Lax"
    assert SESSION_COOKIE.domain is None
    assert SESSION_COOKIE.max_age_seconds == 43_200


def test_cookie_lifetime_tracks_the_absolute_cap_not_the_idle_timeout() -> None:
    absolute_seconds = int(SESSION_POLICY.absolute_lifetime.total_seconds())
    idle_seconds = int(SESSION_POLICY.idle_timeout.total_seconds())
    assert SESSION_COOKIE.max_age_seconds == absolute_seconds
    assert SESSION_COOKIE.max_age_seconds != idle_seconds


@pytest.mark.parametrize(
    ("app_env", "expected"),
    [
        ("development", False),
        ("testing", False),
        ("staging", True),
        ("production", True),
    ],
)
def test_secure_cookie_flag_follows_the_environment(
    app_env: str, expected: bool
) -> None:
    assert SESSION_COOKIE.secure_for(app_env) is expected
    assert AppSettings(app_env=app_env).session_cookie_secure is expected  # type: ignore[arg-type]


def test_secure_environments_are_exactly_staging_and_production() -> None:
    assert SECURE_COOKIE_ENVIRONMENTS == frozenset({"staging", "production"})


@pytest.mark.parametrize(
    "bad_env", ["", "prod", "production ", "Production", "test", None, 1]
)
def test_unknown_environment_is_rejected_rather_than_downgraded(
    bad_env: object,
) -> None:
    with pytest.raises(ConfigError):
        SESSION_COOKIE.secure_for(bad_env)  # type: ignore[arg-type]


def test_directly_constructed_settings_refuse_an_unknown_environment_at_use() -> None:
    with pytest.raises(ConfigError):
        AppSettings(app_env="prod").session_cookie_secure  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "va session"},
        {"name": "va;session"},
        {"name": "a" * 101},
        {"name": "__Host-va_session"},
        {"name": "__Secure-va_session"},
        {"path": ""},
        {"path": "api/v1"},
        {"path": "/api/v1/"},
        {"path": "/api//v1"},
        {"path": "/api/v1;secure"},
        {"path": "/api/v1?x=1"},
        {"path": "/api/v1\x00"},
        {"http_only": False},
        {"http_only": "true"},
        {"same_site": "Laxx"},
        {"same_site": "lax"},
        {"same_site": "Lax "},
        {"same_site": "Strict"},
        {"same_site": "None"},
        {"same_site": None},
        {"max_age_seconds": 0},
        {"max_age_seconds": -1},
        {"max_age_seconds": 59},
        {"max_age_seconds": 3_600},
        {"max_age_seconds": 43_199},
        {"max_age_seconds": 43_201},
        {"max_age_seconds": 86_400},
        {"max_age_seconds": 43_200.0},
        {"max_age_seconds": "43200"},
        {"max_age_seconds": True},
        {"max_age_seconds": 2_592_001},
        {"domain": ""},
        {"domain": "example.com"},
        {"domain": "sub.example.com"},
        {"domain": ".example.com"},
        {"domain": "example.com:443"},
        {"domain": "https://example.com"},
        {"domain": "Example.com"},
        {"domain": 123},
    ],
)
def test_invalid_cookie_configuration_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SessionCookieConfig(**kwargs)  # type: ignore[arg-type]


def test_valid_non_default_cookie_name_and_path_are_accepted() -> None:
    cookie = SessionCookieConfig(name="attendance_session", path="/api")
    assert cookie.name == "attendance_session"
    assert cookie.path == "/api"
    assert cookie.same_site == "Lax"
    assert cookie.max_age_seconds == 43_200
    assert cookie.domain is None


def test_cookie_domain_must_remain_unset() -> None:
    """Approved policy: no ``Domain`` attribute, with no override mechanism."""
    assert SessionCookieConfig(domain=None).domain is None

    # A syntactically valid hostname is still refused: the policy is "unset",
    # not "well-formed Domain".
    with pytest.raises(ValueError):
        SessionCookieConfig(domain="attendance.example.com")

    # Invalid hostnames are refused for the same reason.
    for invalid in (".example.com", "example.com:443", "https://x", "", 123):
        with pytest.raises(ValueError):
            SessionCookieConfig(domain=invalid)  # type: ignore[arg-type]


def test_same_site_is_exactly_lax() -> None:
    """Approved policy: exactly ``Lax``; no configurable override."""
    assert SessionCookieConfig(same_site="Lax").same_site == "Lax"

    for refused in ("Strict", "None", "lax", "LAX", "Lax ", "", None, 1):
        with pytest.raises(ValueError):
            SessionCookieConfig(same_site=refused)  # type: ignore[arg-type]


def test_cookie_lifetime_is_enforced_for_every_instance() -> None:
    """The 43,200 s lifetime is a per-instance invariant, not a singleton check."""
    absolute_seconds = int(SESSION_POLICY.absolute_lifetime.total_seconds())
    assert absolute_seconds == 43_200
    assert SessionCookieConfig(max_age_seconds=absolute_seconds).max_age_seconds == 43_200

    for refused in (
        absolute_seconds - 1,  # shorter
        absolute_seconds + 1,  # longer
        3_600,
        86_400,
        0,
        -1,
    ):
        with pytest.raises(ValueError):
            SessionCookieConfig(max_age_seconds=refused)

    # Numeric type validation still runs before the equality check.
    for malformed in (43_200.0, "43200", True, None):
        with pytest.raises(ValueError):
            SessionCookieConfig(max_age_seconds=malformed)  # type: ignore[arg-type]


def test_login_throttle_defaults_are_the_approved_ones() -> None:
    assert LOGIN_THROTTLE.max_failed_attempts == 5
    assert LOGIN_THROTTLE.window == timedelta(minutes=15)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_failed_attempts": 0},
        {"max_failed_attempts": -1},
        {"max_failed_attempts": 101},
        {"max_failed_attempts": 5.0},
        {"max_failed_attempts": "5"},
        {"max_failed_attempts": True},
        {"window": timedelta(0)},
        {"window": timedelta(seconds=-1)},
        {"window": 900},
        {"window": "15m"},
        {"window": timedelta(hours=25)},
    ],
)
def test_invalid_throttle_configuration_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        LoginThrottleConfig(**kwargs)  # type: ignore[arg-type]


def test_auth_configuration_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        SESSION_COOKIE.name = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        SESSION_COOKIE.path = "/"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        LOGIN_THROTTLE.max_failed_attempts = 10  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        LOGIN_THROTTLE.window = timedelta(hours=1)  # type: ignore[misc]


def test_password_policy_is_unchanged_by_this_milestone() -> None:
    assert PASSWORD_HASHING.memory_cost_kib == 19456
    assert PASSWORD_HASHING.time_cost == 2
    assert PASSWORD_HASHING.parallelism == 1
    assert PASSWORD_HASHING.salt_length == 16
    assert PASSWORD_HASHING.hash_length == 32
    assert PASSWORD_HASHING.min_password_length == 8
    assert PASSWORD_HASHING.max_password_length == 1024


def test_no_client_controlled_or_security_disable_switch_exists() -> None:
    """Request-ID trust, CSRF disablement and proxy-header trust must never
    become configuration: the approved design has no such switch."""
    public_names = {name.upper() for name in dir(config_module)}
    forbidden_fragments = ("FORWARDED", "REQUEST_ID", "CSRF", "LOCKOUT")
    assert not any(
        fragment in name
        for name in public_names
        for fragment in forbidden_fragments
    )


# ---------------------------------------------------------------------------
# Trusted login origins (P5.4.5)
# ---------------------------------------------------------------------------


def test_trusted_origins_are_exact_per_environment() -> None:
    from src.config import TRUSTED_ORIGINS

    assert TRUSTED_ORIGINS["development"] == ("http://localhost:3000",)
    assert TRUSTED_ORIGINS["testing"] == ()
    assert TRUSTED_ORIGINS["staging"] == ()
    assert TRUSTED_ORIGINS["production"] == ()
    assert "*" not in str(TRUSTED_ORIGINS)


def test_default_settings_select_the_environment_allowlist() -> None:
    from src.config import AppSettings

    assert AppSettings().login_trusted_origins == ("http://localhost:3000",)
    for environment in ("testing", "staging", "production"):
        assert AppSettings(app_env=environment).login_trusted_origins == ()  # type: ignore[arg-type]


def test_injected_allowlist_is_used_verbatim() -> None:
    from src.config import AppSettings

    settings = AppSettings(
        app_env="testing",
        login_trusted_origins=("https://attendance.test.invalid",),
    )
    assert settings.login_trusted_origins == ("https://attendance.test.invalid",)
    assert AppSettings(app_env="staging").login_trusted_origins == ()


def test_allowlist_accepts_canonical_equivalent_spellings() -> None:
    from src.config import AppSettings

    settings = AppSettings(
        app_env="testing",
        login_trusted_origins=("HTTPS://Attendance.Test.Invalid:443/",),
    )
    assert settings.login_trusted_origins == ("HTTPS://Attendance.Test.Invalid:443/",)


@pytest.mark.parametrize(
    "bad",
    [
        "*",
        "https://attendance.test.invalid/login",
        "https://attendance.test.invalid?x=1",
        "https://user:pass@attendance.test.invalid",
        "ftp://attendance.test.invalid",
        "not-an-origin",
        "",
    ],
)
def test_invalid_injected_origins_are_refused_at_construction(bad: str) -> None:
    from src.config import AppSettings, ConfigError

    with pytest.raises(ConfigError):
        AppSettings(app_env="testing", login_trusted_origins=(bad,))


def test_duplicate_injected_origins_are_refused() -> None:
    from src.config import AppSettings, ConfigError

    with pytest.raises(ConfigError):
        AppSettings(
            app_env="testing",
            login_trusted_origins=(
                "https://attendance.test.invalid",
                "https://ATTENDANCE.test.invalid:443",
            ),
        )


def test_trusted_origins_must_be_a_tuple() -> None:
    from src.config import AppSettings, ConfigError

    with pytest.raises(ConfigError):
        AppSettings(
            app_env="testing",
            login_trusted_origins=["https://attendance.test.invalid"],  # type: ignore[arg-type]
        )


def test_environment_supplies_staging_and_production_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deployments name their exact origin: no invented URL, no wildcard."""
    from src.config import load_settings

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv(
        "ATTENDANCE_TRUSTED_ORIGINS", "https://dash.staging.example.com"
    )

    settings = load_settings()

    assert settings.app_env == "staging"
    assert settings.login_trusted_origins == ("https://dash.staging.example.com",)
    assert "*" not in str(settings.login_trusted_origins)


def test_environment_origin_list_is_split_and_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import load_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(
        "ATTENDANCE_TRUSTED_ORIGINS",
        " https://dash.example.com , https://admin.example.com:8443 ",
    )

    assert load_settings().login_trusted_origins == (
        "https://dash.example.com",
        "https://admin.example.com:8443",
    )


def test_malformed_environment_origin_is_refused_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import ConfigError, load_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ATTENDANCE_TRUSTED_ORIGINS", "*")

    with pytest.raises(ConfigError):
        load_settings()


def test_blank_environment_origin_list_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import load_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ATTENDANCE_TRUSTED_ORIGINS", "   ")

    assert load_settings().login_trusted_origins == ()


def test_unset_environment_origin_list_keeps_the_fail_closed_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import load_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("ATTENDANCE_TRUSTED_ORIGINS", raising=False)

    assert load_settings().login_trusted_origins == ()
