import { EVENT_TYPE_CHECK_IN } from "../api/types";
import type { AttendanceEvent, Employee } from "../api/types";
import {
  foldLatestEvents,
  parseInstant,
  utcDayKey,
} from "../attendance/journal";

// The instant/day helpers live with the shared journal logic now; they stay
// exported from here so existing consumers and tests keep working unchanged.
export { parseInstant, utcDayKey };

/**
 * Pure Overview calculations.
 *
 * Everything here is derived from verified response fields only. There is no
 * attendance rate, no trend, no score and no estimate: the backend exposes
 * journal events and employee records, and nothing more is invented.
 */

export interface EmployeeTotals {
  readonly total: number;
  readonly active: number;
  readonly inactive: number;
}

export function summariseEmployees(
  employees: readonly Employee[],
): EmployeeTotals {
  let active = 0;
  for (const employee of employees) {
    if (employee.is_active) {
      active += 1;
    }
  }
  return {
    total: employees.length,
    active,
    inactive: employees.length - active,
  };
}

/**
 * Events whose `occurred_at` falls on the **UTC** day of `nowMs`.
 *
 * The metric is labelled "Events today (UTC)" precisely because this is not the
 * viewer's local calendar day.
 */
export function countEventsOnUtcDay(
  events: readonly AttendanceEvent[],
  nowMs: number,
): number {
  const todaysKey = utcDayKey(nowMs);
  let count = 0;
  for (const event of events) {
    const instant = parseInstant(event.occurred_at);
    if (Number.isNaN(instant)) {
      continue;
    }
    if (utcDayKey(instant) === todaysKey) {
      count += 1;
    }
  }
  return count;
}

export interface SessionSummary {
  /** Employees whose latest session boundary is a check-in. */
  readonly openSessions: number;
  /** Distinct employees with at least one classified, dated event. */
  readonly employeesWithEvents: number;
  /** Events with an unreadable timestamp (excluded from the fold). */
  readonly undatedEvents: number;
  /** Events whose type is neither check-in nor check-out (excluded). */
  readonly unclassifiedEvents: number;
}

/**
 * Fold the journal into one latest session boundary per employee.
 *
 * Ordering is `(occurred_at, journal position)`: a later timestamp wins, and on
 * an exact tie the later journal position wins — the same `>=` rule the backend
 * uses when it restores its open-session marker, so the two agree.
 * An employee is counted at most once, and an unreadable timestamp or an
 * unknown event type never creates a session.
 *
 * The fold itself is shared with the Attendance page (`foldLatestEvents`); this
 * wrapper keeps the Overview's public shape and meanings unchanged.
 */
export function summariseSessions(
  events: readonly AttendanceEvent[],
): SessionSummary {
  const fold = foldLatestEvents(events);

  let openSessions = 0;
  for (const boundary of fold.latest.values()) {
    if (boundary.event.event_type === EVENT_TYPE_CHECK_IN) {
      openSessions += 1;
    }
  }

  return {
    openSessions,
    employeesWithEvents: fold.latest.size,
    undatedEvents: fold.undatedEvents,
    unclassifiedEvents: fold.unclassifiedEvents,
  };
}

export interface OverviewMetrics {
  readonly employeeTotals: EmployeeTotals;
  readonly eventsToday: number;
  readonly sessions: SessionSummary;
  readonly utcDay: string;
  readonly incompleteTrailingRecord: boolean;
}

/** Combine both sections for the Overview render (each may be unavailable). */
export function buildOverviewMetrics(
  employees: readonly Employee[],
  attendance: {
    readonly events: readonly AttendanceEvent[];
    readonly incomplete_trailing_record: boolean;
  },
  nowMs: number,
): OverviewMetrics {
  return {
    employeeTotals: summariseEmployees(employees),
    eventsToday: countEventsOnUtcDay(attendance.events, nowMs),
    sessions: summariseSessions(attendance.events),
    utcDay: utcDayKey(nowMs),
    incompleteTrailingRecord: attendance.incomplete_trailing_record,
  };
}
