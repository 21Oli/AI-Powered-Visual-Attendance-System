"""P5.4.9 unit tests for the process-local login throttle.

Deterministic by construction: the throttle takes a monotonic clock callable
and every test injects a fake, so no test waits for a real window to elapse.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from src.config import LOGIN_THROTTLE, LoginThrottleConfig
from src.login_throttle import (
    INVALID_USERNAME_KEY,
    LoginRateLimited,
    LoginThrottle,
    throttle_key_for,
)
from src.user import UserValidationError, username_key_for

WINDOW_SECONDS = 900.0


class FakeMonotonic:
    """Deterministic monotonic clock (seconds since an arbitrary origin)."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(**kwargs: object) -> tuple[LoginThrottle, FakeMonotonic]:
    clock = FakeMonotonic()
    options: dict[str, object] = {"clock": clock}
    options.update(kwargs)
    return LoginThrottle(**options), clock  # type: ignore[arg-type]


def exhaust(throttle: LoginThrottle, username: str, attempts: int = 5) -> None:
    for _ in range(attempts):
        throttle.record_failure(throttle.begin_attempt(username))


# ---------------------------------------------------------------------------
# Allowed / counted / blocked
# ---------------------------------------------------------------------------


def test_a_new_username_is_initially_allowed() -> None:
    throttle, _ = build()
    assert throttle.retry_after("ada") is None
    assert throttle.is_limited("ada") is False
    throttle.begin_attempt("ada")  # does not raise


def test_the_first_failed_attempt_is_recorded_without_blocking() -> None:
    throttle, _ = build()
    throttle.begin_attempt("ada")
    throttle.record_failure("ada")

    assert throttle.tracked_keys == 1
    assert throttle.retry_after("ada") is None


def test_five_failed_attempts_are_allowed_and_the_sixth_is_blocked() -> None:
    throttle, _ = build()

    exhaust(throttle, "ada")

    assert throttle.tracked_keys == 1
    assert throttle.retry_after("ada") == 900
    with pytest.raises(LoginRateLimited) as raised:
        throttle.begin_attempt("ada")
    assert raised.value.retry_after_seconds == 900


def test_retry_after_counts_down_with_the_monotonic_clock() -> None:
    throttle, clock = build()
    exhaust(throttle, "ada")

    clock.advance(1)
    assert throttle.retry_after("ada") == 899
    clock.advance(59)
    assert throttle.retry_after("ada") == 840
    clock.advance(839.5)
    assert throttle.retry_after("ada") == 1, "a sub-second remainder rounds up"


# ---------------------------------------------------------------------------
# Window expiry and boundaries
# ---------------------------------------------------------------------------


def test_window_expires_after_fifteen_minutes() -> None:
    throttle, clock = build()
    exhaust(throttle, "ada")

    clock.advance(WINDOW_SECONDS - 0.001)
    assert throttle.retry_after("ada") == 1, "still blocked just before the end"

    clock.advance(0.001)  # exactly now == window_start + window
    assert throttle.retry_after("ada") is None
    throttle.begin_attempt("ada")


def test_an_expired_username_can_attempt_and_starts_a_new_window() -> None:
    throttle, clock = build()
    exhaust(throttle, "ada")
    clock.advance(WINDOW_SECONDS)

    throttle.begin_attempt("ada")
    throttle.record_failure("ada")
    assert throttle.retry_after("ada") is None, "one failure is a fresh window"

    for _ in range(4):
        throttle.begin_attempt("ada")
        throttle.record_failure("ada")
    assert throttle.retry_after("ada") == 900


def test_no_permanent_lockout_after_many_windows() -> None:
    throttle, clock = build()
    for _ in range(3):
        exhaust(throttle, "ada")
        with pytest.raises(LoginRateLimited):
            throttle.begin_attempt("ada")
        clock.advance(WINDOW_SECONDS)
        assert throttle.retry_after("ada") is None


# ---------------------------------------------------------------------------
# Reset and isolation
# ---------------------------------------------------------------------------


def test_successful_authentication_resets_the_counter() -> None:
    throttle, _ = build()
    exhaust(throttle, "ada")
    assert throttle.retry_after("ada") == 900

    throttle.record_success("ada")

    assert throttle.retry_after("ada") is None
    assert throttle.tracked_keys == 0
    throttle.begin_attempt("ada")


def test_failures_for_one_username_do_not_affect_another() -> None:
    throttle, _ = build()
    exhaust(throttle, "ada")

    assert throttle.retry_after("ada") == 900
    assert throttle.retry_after("grace") is None
    throttle.begin_attempt("grace")


def test_success_on_one_username_does_not_clear_another() -> None:
    throttle, _ = build()
    exhaust(throttle, "ada")
    exhaust(throttle, "grace")

    throttle.record_success("grace")

    assert throttle.retry_after("grace") is None
    assert throttle.retry_after("ada") == 900


# ---------------------------------------------------------------------------
# Normalization (one policy, shared with the login service)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["ada", " Ada ", "ADA", "  ada  ", "ＡＤＡ"],
)
def test_normalization_matches_the_login_lookup_key(raw: str) -> None:
    assert throttle_key_for(raw) == username_key_for(raw)
    assert throttle_key_for(raw) == username_key_for("ada")


def test_case_and_whitespace_variants_share_one_counter() -> None:
    throttle, _ = build()
    for _ in range(5):
        throttle.begin_attempt("  Ada ")
        throttle.record_failure("  Ada ")

    assert throttle.retry_after("ADA") == 900
    with pytest.raises(LoginRateLimited):
        throttle.begin_attempt("ada")


def test_usernames_that_cannot_form_a_key_share_one_isolated_bucket() -> None:
    throttle, _ = build()
    with pytest.raises(UserValidationError):
        username_key_for("ab")

    assert throttle_key_for("ab") == INVALID_USERNAME_KEY
    exhaust(throttle, "ab")

    assert throttle.retry_after("ab") == 900
    assert throttle.retry_after("xy") == 900, "same invalid bucket"
    assert throttle.retry_after("ada") is None, "never blocks a real account"


# ---------------------------------------------------------------------------
# Concurrency: the reservation closes the check-then-record race
# ---------------------------------------------------------------------------


def test_concurrent_attempts_cannot_exceed_the_limit() -> None:
    throttle, _ = build()
    barrier = threading.Barrier(20)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            throttle.begin_attempt("ada")
        except LoginRateLimited:
            with lock:
                outcomes.append("limited")
        else:
            # The reservation is held (not completed) so the test observes the
            # real concurrent window; releasing it immediately would let every
            # thread through one after another.
            with lock:
                outcomes.append("reserved")

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("reserved") == LOGIN_THROTTLE.max_failed_attempts
    assert outcomes.count("limited") == 20 - LOGIN_THROTTLE.max_failed_attempts


def test_reserved_attempts_count_toward_the_limit_before_they_fail() -> None:
    throttle, _ = build()
    for _ in range(4):
        throttle.begin_attempt("ada")
        throttle.record_failure("ada")
    throttle.begin_attempt("ada")  # fifth slot reserved, result unknown yet

    with pytest.raises(LoginRateLimited):
        throttle.begin_attempt("ada")


# ---------------------------------------------------------------------------
# Reservation lifecycle and infrastructure failures
# ---------------------------------------------------------------------------


def test_aborting_a_reservation_does_not_consume_an_attempt() -> None:
    throttle, _ = build()
    for _ in range(10):
        throttle.begin_attempt("ada")
        throttle.abort_attempt("ada")

    assert throttle.retry_after("ada") is None
    assert throttle.tracked_keys == 0, "a bare reservation leaves no state"


def test_aborting_after_a_failure_keeps_the_failure() -> None:
    throttle, _ = build()
    throttle.begin_attempt("ada")
    throttle.record_failure("ada")
    throttle.begin_attempt("ada")
    throttle.abort_attempt("ada")

    assert throttle.tracked_keys == 1
    assert throttle.retry_after("ada") is None


def test_one_aborted_reservation_leaves_no_residual_state() -> None:
    throttle, _ = build()
    reservation = throttle.begin_attempt("ada")

    throttle.abort_attempt(reservation)

    assert throttle.tracked_keys == 0
    assert throttle.retry_after("ada") is None
    exhaust(throttle, "ada")
    assert throttle.retry_after("ada") == 900


def test_four_failures_then_one_aborted_reservation_still_allow_the_fifth() -> None:
    throttle, _ = build()
    exhaust(throttle, "ada", attempts=4)

    throttle.abort_attempt(throttle.begin_attempt("ada"))
    assert throttle.retry_after("ada") is None, "the abort released its slot"

    throttle.record_failure(throttle.begin_attempt("ada"))
    assert throttle.retry_after("ada") == 900, "the fifth real failure blocks"


def test_repeated_infrastructure_failures_never_block_a_user() -> None:
    throttle, _ = build()
    for _ in range(250):
        throttle.abort_attempt(throttle.begin_attempt("ada"))

    assert throttle.tracked_keys == 0
    assert throttle.retry_after("ada") is None
    exhaust(throttle, "ada")
    assert throttle.retry_after("ada") == 900


def test_multiple_simultaneous_reservations_can_be_aborted_independently() -> None:
    throttle, _ = build()
    first = throttle.begin_attempt("ada")
    second = throttle.begin_attempt("ada")
    third = throttle.begin_attempt("ada")
    fourth = throttle.begin_attempt("ada")

    throttle.abort_attempt(first)
    throttle.abort_attempt(third)

    assert throttle.tracked_keys == 1, "the entry still holds two reservations"
    for _ in range(3):
        throttle.begin_attempt("ada")
    with pytest.raises(LoginRateLimited):
        throttle.begin_attempt("ada")

    throttle.abort_attempt(second)
    throttle.abort_attempt(fourth)
    assert throttle.retry_after("ada") is None


def test_aborting_one_requests_reservation_does_not_release_another() -> None:
    throttle, _ = build()
    mine = throttle.begin_attempt("ada")
    throttle.begin_attempt("ada")  # another request's live reservation

    throttle.abort_attempt(mine)

    for _ in range(4):
        throttle.begin_attempt("ada")
    # The other request's reservation is still held, so the limit is reached.
    with pytest.raises(LoginRateLimited):
        throttle.begin_attempt("ada")


def test_stale_reservation_cannot_touch_a_new_window() -> None:
    throttle, clock = build()
    stale = throttle.begin_attempt("ada")
    clock.advance(WINDOW_SECONDS)
    throttle.begin_attempt("ada")  # new window, one live reservation

    throttle.abort_attempt(stale)

    for _ in range(4):
        throttle.begin_attempt("ada")
    # The new window's slot is untouched by the stale abort.
    with pytest.raises(LoginRateLimited):
        throttle.begin_attempt("ada")


def test_stale_failure_counts_in_the_current_window_without_releasing_a_slot() -> None:
    throttle, clock = build()
    stale = throttle.begin_attempt("ada")
    clock.advance(WINDOW_SECONDS)
    throttle.begin_attempt("ada")  # new window, one live reservation

    throttle.record_failure(stale)  # the guess still happened in this window

    assert throttle.tracked_keys == 1
    for _ in range(3):
        throttle.begin_attempt("ada")
    # One counted failure plus one live reservation already consume two slots.
    with pytest.raises(LoginRateLimited):
        throttle.begin_attempt("ada")


def test_eviction_does_not_let_a_stale_reservation_release_a_new_entry() -> None:
    throttle, _ = build(max_tracked_keys=1)
    stale = throttle.begin_attempt("ada")
    throttle.begin_attempt("grace")  # evicts ada under the cap

    throttle.abort_attempt(stale)
    assert throttle.tracked_keys == 1

    throttle.begin_attempt("ada")  # a brand-new entry for the same username
    for _ in range(4):
        throttle.begin_attempt("ada")
    # The recreated entry keeps the reservation it issued.
    with pytest.raises(LoginRateLimited):
        throttle.begin_attempt("ada")


def test_abort_after_a_successful_reset_leaves_no_state() -> None:
    throttle, _ = build()
    stale = throttle.begin_attempt("ada")

    throttle.record_success(stale)  # successful sign-in drops the window
    throttle.abort_attempt(stale)

    assert throttle.tracked_keys == 0
    assert throttle.retry_after("ada") is None


def test_reservations_are_bound_to_their_window_and_hide_the_key() -> None:
    throttle, clock = build()
    first = throttle.begin_attempt("ada")
    second = throttle.begin_attempt("ada")
    assert first.generation == second.generation, "same window, same entry"

    clock.advance(WINDOW_SECONDS)
    third = throttle.begin_attempt("ada")
    assert third.generation != first.generation, "a new window is a new entry"
    assert "ada" not in repr(first)
    assert first.key == third.key


def test_record_failure_without_a_reservation_still_counts() -> None:
    throttle, _ = build()
    for _ in range(5):
        throttle.record_failure("ada")

    assert throttle.retry_after("ada") == 900


# ---------------------------------------------------------------------------
# Memory bounds
# ---------------------------------------------------------------------------


def test_expired_entries_are_purged() -> None:
    throttle, clock = build()
    exhaust(throttle, "ada")
    exhaust(throttle, "grace")
    assert throttle.tracked_keys == 2

    clock.advance(WINDOW_SECONDS)
    throttle.begin_attempt("linus")

    assert throttle.tracked_keys == 1, "expired windows were dropped"


def test_tracked_keys_are_bounded() -> None:
    throttle, _ = build(max_tracked_keys=3)

    for index in range(10):
        throttle.begin_attempt(f"user-{index}")
        throttle.record_failure(f"user-{index}")

    assert throttle.tracked_keys <= 3


def test_eviction_cannot_block_an_unrelated_new_user() -> None:
    throttle, _ = build(max_tracked_keys=2)
    exhaust(throttle, "ada")
    exhaust(throttle, "grace")
    exhaust(throttle, "linus")

    assert throttle.retry_after("newcomer") is None
    throttle.begin_attempt("newcomer")


# ---------------------------------------------------------------------------
# Configuration, clock discipline and invariants
# ---------------------------------------------------------------------------


def test_default_policy_is_the_approved_one() -> None:
    throttle, _ = build()
    assert throttle._max_attempts == 5  # noqa: SLF001 - pinned policy
    assert throttle._window_seconds == 900.0  # noqa: SLF001 - pinned policy


def test_custom_configuration_is_honoured() -> None:
    config = LoginThrottleConfig(
        max_failed_attempts=2, window=timedelta(seconds=30)
    )
    throttle, clock = build(config=config)

    for _ in range(2):
        throttle.begin_attempt("ada")
        throttle.record_failure("ada")

    assert throttle.retry_after("ada") == 30
    clock.advance(30)
    assert throttle.retry_after("ada") is None


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "5"])
def test_invalid_key_cap_is_refused(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        LoginThrottle(max_tracked_keys=bad)  # type: ignore[arg-type]


def test_non_callable_clock_is_refused() -> None:
    with pytest.raises(TypeError):
        LoginThrottle(clock=object())  # type: ignore[arg-type]


def test_state_is_local_to_the_instance() -> None:
    first, _ = build()
    second, _ = build()
    exhaust(first, "ada")

    assert first.retry_after("ada") == 900
    assert second.retry_after("ada") is None


def test_expiry_never_depends_on_wall_clock_time() -> None:
    """The throttle reads only the injected monotonic clock."""

    readings: list[float] = []

    def clock() -> float:
        readings.append(1_000.0)
        return 1_000.0

    throttle = LoginThrottle(clock=clock)
    exhaust(throttle, "ada")

    assert readings, "the clock is the only time source"
    assert throttle.retry_after("ada") == 900
