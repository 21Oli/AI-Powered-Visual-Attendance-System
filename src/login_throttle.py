"""Process-local fixed-window login throttle (P5.4.9).

``POST /api/v1/auth/login`` accepts a password, so repeated guesses must be
slowed down. This module owns **only** that accounting: it counts failed
attempts per normalized username, reports when a key is temporarily blocked,
and forgets a key after its window expires or its owner signs in successfully.
It never sees a password, hash, session token or repository, and it has no
database, network or logging dependency.

Policy (owner-approved, :data:`src.config.LOGIN_THROTTLE`)
---------------------------------------------------------

* **Fixed window**, not a sliding one: a key's window starts at its first
  failure and ends exactly ``window`` seconds later.
* **Five failed attempts per window.** The first five are processed normally;
  from the fifth failure onward attempts are refused until the window ends.
* **Scope is the normalized username** (:func:`throttle_key_for`, the same
  ``username_key_for`` the repository uses), so ``"  Ada "`` and ``"ada"``
  share one counter and different users never affect each other. Nonexistent
  and malformed usernames are limited exactly like real ones, so the limiter
  cannot be used to discover which accounts exist.
* **No permanent lockout**: nothing survives the window, and a successful
  sign-in clears the key immediately.
* **Process-local**: counters live in this process's memory. Two worker
  processes have independent counters — that is a documented limitation, not
  distributed rate limiting.

Correctness properties
----------------------

* **Monotonic clock.** Elapsed time comes from :func:`time.monotonic` (or an
  injected zero-argument float clock); wall-clock changes and NTP steps cannot
  extend or collapse a window. Tests inject a fake clock, so none of them
  sleeps for fifteen minutes.
* **No concurrent bypass.** :meth:`LoginThrottle.begin_attempt` *reserves* a
  slot under the same lock that checks the limit, so ``failures + in_flight``
  can never exceed the configured maximum: ten simultaneous wrong guesses
  still produce at most five password verifications. The reservation is
  completed by :meth:`record_failure`, :meth:`record_success` or
  :meth:`abort_attempt`.
* **Every reservation is finalised against its own window.**
  :meth:`LoginThrottle.begin_attempt` returns a :class:`Reservation` carrying
  the key's *generation* — a counter bumped each time an entry is created. A
  reservation is released only on the entry that issued it: if the window
  expired, the key was evicted by the cap, or a successful sign-in reset the
  key while a request was still verifying, finalising that stale reservation
  is a no-op for the old entry and never touches a newer entry for the same
  username. A failed guess from a stale reservation is still counted in the
  current window, because the guess did happen there; it just cannot release
  a slot another request holds.
* **Infrastructure failures are not guesses.** A caller aborts its
  reservation when the database or an internal service fails, so an outage
  never consumes a user's attempts or produces a 429.
* **Bounded memory.** Expired entries are purged on every mutation; above a
  cap the oldest windows are evicted. Eviction can only ever *forgive* a
  limiter (fail open) and never block an unrelated user.

The throttle keeps no mutable module-level state: the application creates one
instance and injects it into the login service, matching the existing
dependency-injection pattern.
"""

from __future__ import annotations

import itertools
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Final

from src.config import LOGIN_THROTTLE, LoginThrottleConfig
from src.user import UserValidationError, username_key_for

__all__ = [
    "INVALID_USERNAME_KEY",
    "LOGIN_RATE_LIMITED_CODE",
    "LOGIN_RATE_LIMITED_MESSAGE",
    "LoginRateLimited",
    "LoginThrottle",
    "Reservation",
    "throttle_key_for",
]

#: Stable machine code for the sanitized 429 envelope.
LOGIN_RATE_LIMITED_CODE: str = "LOGIN_RATE_LIMITED"
#: Fixed human message. Never names a username, an account or the limiter state.
LOGIN_RATE_LIMITED_MESSAGE: str = (
    "Too many failed sign-in attempts. Try again later."
)

#: Shared bucket for usernames that cannot form a lookup key (e.g. ``"ab"``).
#: Such input is still limited, but it can never collide with a real account's
#: key and can never block one.
INVALID_USERNAME_KEY: Final[str] = "\x00invalid-username"

#: Default cap on tracked keys (an operational bound, not a policy setting).
_DEFAULT_MAX_TRACKED_KEYS: Final[int] = 4_096


class LoginRateLimited(Exception):
    """The key is temporarily blocked after too many failed attempts.

    Carries only the whole-second ``Retry-After`` value the client needs; no
    username, account existence, attempt count or window start is exposed.
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many failed sign-in attempts")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


def throttle_key_for(username: object) -> str:
    """Return the limiter key for ``username`` using the login normalization.

    Exactly :func:`src.user.username_key_for` — the same function the user
    repository uses — so there is one normalization policy, not two. A value
    that cannot form a key (too short, control characters, not a string) maps
    to :data:`INVALID_USERNAME_KEY`.
    """
    try:
        return username_key_for(username)  # type: ignore[arg-type]
    except (UserValidationError, TypeError):
        return INVALID_USERNAME_KEY


@dataclass(slots=True)
class _Entry:
    """One key's fixed window.

    ``generation`` identifies this specific window. It is unique per instance
    and bumped whenever a new entry replaces an old one (expiry, eviction,
    reset), so a reservation taken in an older window can be recognised as
    stale instead of being applied to its successor.
    """

    window_start: float
    failures: int = 0
    in_flight: int = 0
    generation: int = 0


@dataclass(frozen=True, slots=True)
class Reservation:
    """Handle for exactly one in-flight attempt (P5.4.9).

    Returned by :meth:`LoginThrottle.begin_attempt` and passed back to
    :meth:`LoginThrottle.record_failure`, :meth:`LoginThrottle.record_success`
    or :meth:`LoginThrottle.abort_attempt`. The generation ties the handle to
    the window entry it reserved in, so releasing it can never decrement a
    different request's reservation (or a newer window's). The key is excluded
    from ``repr`` so a handle can never print a username-derived value into a
    log by accident.
    """

    key: str = field(repr=False)
    generation: int


class LoginThrottle:
    """Fixed-window failed-attempt counter for login (see module docstring).

    Parameters
    ----------
    config:
        Attempt maximum and window length; defaults to the approved
        :data:`src.config.LOGIN_THROTTLE` (5 attempts / 15 minutes).
    clock:
        Zero-argument callable returning **monotonic** seconds. Defaults to
        :func:`time.monotonic`; tests inject a deterministic fake.
    max_tracked_keys:
        Upper bound on remembered keys. Expired entries are purged first and
        the oldest windows are evicted beyond the cap, so memory is bounded.
    """

    def __init__(
        self,
        *,
        config: LoginThrottleConfig = LOGIN_THROTTLE,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_keys: int = _DEFAULT_MAX_TRACKED_KEYS,
    ) -> None:
        if not isinstance(config, LoginThrottleConfig):
            raise TypeError(
                "config must be a LoginThrottleConfig, got "
                f"{type(config).__name__}"
            )
        if not callable(clock):
            raise TypeError("clock must be a zero-argument callable")
        if isinstance(max_tracked_keys, bool) or not isinstance(
            max_tracked_keys, int
        ):
            raise TypeError(
                "max_tracked_keys must be a plain int (bool rejected), got "
                f"{type(max_tracked_keys).__name__}"
            )
        if max_tracked_keys < 1:
            raise ValueError(f"max_tracked_keys must be >= 1, got {max_tracked_keys}")

        self._max_attempts = config.max_failed_attempts
        self._window_seconds = float(config.window.total_seconds())
        self._clock = clock
        self._max_tracked_keys = max_tracked_keys
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._generations = itertools.count(1)

    # -- internals (the caller must hold the lock) --------------------------

    def _expired(self, entry: _Entry, now: float) -> bool:
        return now >= entry.window_start + self._window_seconds

    def _retry_after_locked(self, key: str, now: float) -> int | None:
        """Seconds until ``key`` is usable again, or ``None`` when it is now."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._expired(entry, now):
            # The window is over: the key starts clean, never permanently.
            del self._entries[key]
            return None
        if entry.failures + entry.in_flight < self._max_attempts:
            return None
        remaining = entry.window_start + self._window_seconds - now
        return max(1, math.ceil(remaining))

    def _target(self, attempt: Reservation | str) -> tuple[str, int | None]:
        """Return ``(key, generation)`` for a reservation or a bare username.

        A bare username (legacy/test callers) means "the current entry for this
        key" and gives up the staleness guarantee; every production call path
        passes the :class:`Reservation` returned by ``begin_attempt``.
        """
        if isinstance(attempt, Reservation):
            return attempt.key, attempt.generation
        return throttle_key_for(attempt), None

    def _prune_locked(self, now: float) -> None:
        """Drop expired windows, then enforce the key cap (oldest first)."""
        expired = [key for key, entry in self._entries.items() if self._expired(entry, now)]
        for key in expired:
            del self._entries[key]

        overflow = len(self._entries) - self._max_tracked_keys
        if overflow <= 0:
            return
        oldest = sorted(self._entries.items(), key=lambda item: item[1].window_start)
        for key, _ in oldest[:overflow]:
            del self._entries[key]

    # -- public surface -----------------------------------------------------

    def retry_after(self, username: object) -> int | None:
        """Return ``Retry-After`` seconds for ``username``, or ``None``.

        Pure observation: it never records, reserves or mutates a live window
        (an expired one is dropped).
        """
        key = throttle_key_for(username)
        with self._lock:
            return self._retry_after_locked(key, self._clock())

    def is_limited(self, username: object) -> bool:
        """Whether ``username`` is currently blocked."""
        return self.retry_after(username) is not None

    def begin_attempt(self, username: object) -> Reservation:
        """Reserve one attempt slot, or raise :class:`LoginRateLimited`.

        Reserving before the password check is what makes the limit hold under
        concurrency: the check and the increment happen under one lock, so
        ``failures + in_flight`` never exceeds the configured maximum. The
        returned :class:`Reservation` must be passed back to exactly one
        finaliser (``record_failure``, ``record_success`` or
        ``abort_attempt``) so the slot is released against the window that
        issued it.
        """
        key = throttle_key_for(username)
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            retry_after = self._retry_after_locked(key, now)
            if retry_after is not None:
                raise LoginRateLimited(retry_after)
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(
                    window_start=now, generation=next(self._generations)
                )
                self._entries[key] = entry
            entry.in_flight += 1
            return Reservation(key=key, generation=entry.generation)

    def record_failure(self, attempt: Reservation | str) -> None:
        """Complete a reserved attempt as a failed credential guess.

        The failure is always counted in the *current* window. A reservation
        whose window has since ended, been evicted or been reset does not
        release a slot in the newer entry — that slot belongs to another
        request — it only adds the failure.
        """
        key, generation = self._target(attempt)
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                # First failure of a new fixed window.
                self._entries[key] = _Entry(
                    window_start=now,
                    failures=1,
                    generation=next(self._generations),
                )
                return
            if generation is not None and generation != entry.generation:
                # Stale reservation: count the guess, keep the current entry's
                # in-flight reservations intact.
                entry.failures += 1
                return
            entry.in_flight = max(0, entry.in_flight - 1)
            entry.failures += 1

    def record_success(self, attempt: Reservation | str) -> None:
        """Forget the key's state: a successful sign-in resets it.

        Accepted for both reservation and bare-username callers; a reset drops
        the whole window, so it applies whatever generation is current.
        """
        key, _ = self._target(attempt)
        with self._lock:
            self._entries.pop(key, None)

    def abort_attempt(self, attempt: Reservation | str) -> None:
        """Release a reserved attempt without counting it.

        Used for infrastructure failures (database unavailable, internal
        error): an outage must not consume a user's attempts, become a 429, or
        leave a dangling reservation that counts against the limit forever.

        The slot is released on exactly one entry. If the reserved window has
        already expired, been evicted or been replaced, there is nothing left
        to release and the newer entry for that username is left untouched —
        its reservations belong to other requests. An entry left with no
        failures and no reservations is removed entirely.
        """
        key, generation = self._target(attempt)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            if self._expired(entry, self._clock()):
                # The reserved window is over: nothing of it survives, and the
                # key must not inherit the stale release.
                del self._entries[key]
                return
            if generation is not None and generation != entry.generation:
                # A different (newer) window for the same username; its
                # in-flight reservations are not ours to release.
                return
            entry.in_flight = max(0, entry.in_flight - 1)
            if entry.failures == 0 and entry.in_flight == 0:
                # A reservation that never became a failure leaves no state.
                del self._entries[key]

    @property
    def tracked_keys(self) -> int:
        """Number of keys currently holding state (bounded; diagnostics only)."""
        with self._lock:
            return len(self._entries)
