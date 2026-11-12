import { EVENT_TYPE_CHECK_IN, EVENT_TYPE_CHECK_OUT } from "../api/types";
import type { AttendanceEvent } from "../api/types";

/**
 * Pure attendance-journal logic shared by the Overview and the Attendance page.
 *
 * Everything here works on the verified journal contract only:
 *
 * * events arrive in **journal append order**, which is not chronological;
 * * `occurred_at` is a UTC ISO-8601 string that may be unreadable;
 * * `event_type` is `"check_in"` / `"check_out"` for everything the backend
 *   writes today, but an unknown value must never invent a session.
 *
 * The fold rule is the backend's own (`src/attendance_recovery.py`
 * `fold_latest_events`): order by `(occurred_at, journal position)`, so a later
 * timestamp wins and an exact tie goes to the event later in the journal.
 */

/** `Date.parse` millisecond value, or `NaN` when the timestamp is unreadable. */
export function parseInstant(value: string): number {
  return Date.parse(value);
}

/** UTC calendar day key (`YYYY-MM-DD`) for an instant in milliseconds. */
export function utcDayKey(instantMs: number): string {
  return new Date(instantMs).toISOString().slice(0, 10);
}

/** Derived per-employee session state — never stored on an event. */
export type SessionState = "open" | "closed";

/** One employee's latest valid session boundary. */
export interface LatestSessionBoundary {
  readonly event: AttendanceEvent;
  /** Parsed `occurred_at` in milliseconds (always a finite number). */
  readonly instant: number;
  /** Zero-based position in the validated journal. */
  readonly position: number;
}

export interface JournalFold {
  /**
   * `employee_id` → latest valid boundary, in first-seen journal order.
   * Employees with no valid boundary are absent — no state is invented.
   */
  readonly latest: ReadonlyMap<string, LatestSessionBoundary>;
  /** Events whose `occurred_at` could not be parsed (excluded from the fold). */
  readonly undatedEvents: number;
  /** Events whose type is neither check-in nor check-out (excluded). */
  readonly unclassifiedEvents: number;
}

function isSessionBoundaryType(eventType: string): boolean {
  return (
    eventType === EVENT_TYPE_CHECK_IN || eventType === EVENT_TYPE_CHECK_OUT
  );
}

/**
 * Fold the journal into one latest valid session boundary per employee.
 *
 * Purity: no I/O, no clock, no mutation of the supplied array or its events,
 * and the same input always produces the same output. Diagnostic counts are
 * separated exactly as before: an unreadable timestamp is counted as undated
 * (even when its type is also unknown), and an unknown type is counted as
 * unclassified — neither ever creates a session.
 */
export function foldLatestEvents(
  events: readonly AttendanceEvent[],
): JournalFold {
  const latest = new Map<string, LatestSessionBoundary>();
  let undatedEvents = 0;
  let unclassifiedEvents = 0;

  events.forEach((event, position) => {
    const instant = parseInstant(event.occurred_at);
    if (Number.isNaN(instant)) {
      undatedEvents += 1;
      return;
    }
    if (!isSessionBoundaryType(event.event_type)) {
      unclassifiedEvents += 1;
      return;
    }

    const current = latest.get(event.employee_id);
    if (
      current === undefined ||
      instant > current.instant ||
      (instant === current.instant && position > current.position)
    ) {
      latest.set(event.employee_id, { event, instant, position });
    }
  });

  return { latest, undatedEvents, unclassifiedEvents };
}

/**
 * Derive each employee's current session state from a fold.
 *
 * `open` means the latest valid boundary is a check-in, `closed` means a
 * check-out. An employee with no valid boundary has no entry at all, so callers
 * cannot mistake "unknown" for "closed".
 */
export function deriveSessionStates(
  fold: JournalFold,
): ReadonlyMap<string, SessionState> {
  const states = new Map<string, SessionState>();
  for (const [employeeId, boundary] of fold.latest) {
    states.set(
      employeeId,
      boundary.event.event_type === EVENT_TYPE_CHECK_IN ? "open" : "closed",
    );
  }
  return states;
}
