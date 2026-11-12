import type { AttendanceEvent } from "../api/types";
import {
  deriveSessionStates,
  foldLatestEvents,
  parseInstant,
  utcDayKey,
} from "./journal";
import type { SessionState } from "./journal";

/**
 * Pure attendance filtering for the Attendance page.
 *
 * Filters are applied to the validated journal snapshot in memory: the backend
 * supports no attendance query parameters, so nothing here can be mistaken for
 * server-side filtering. The source array and its events are never modified and
 * the original journal order is preserved in every result.
 */

/** Event-type filter. `all` includes unrecognised types. */
export type AttendanceEventTypeFilter = "all" | "check_in" | "check_out";

/** Session filter. Session state belongs to the employee, not the event. */
export type AttendanceSessionStateFilter = "all" | SessionState;

export interface AttendanceFilters {
  /** UTC calendar day (`YYYY-MM-DD`), or `null` for every date. */
  readonly utcDay: string | null;
  readonly eventType: AttendanceEventTypeFilter;
  readonly employeeId: string | null;
  readonly sessionState: AttendanceSessionStateFilter;
}

/** No filtering at all — every event, in journal order. */
export const ALL_ATTENDANCE_FILTERS: AttendanceFilters = {
  utcDay: null,
  eventType: "all",
  employeeId: null,
  sessionState: "all",
};

/**
 * Apply every filter, returning a new array in the original journal order.
 *
 * Date: events with an unreadable timestamp are only visible when `utcDay` is
 * `null`, so they can never be attributed to a day they do not have.
 * Session: an employee with no valid boundary is excluded from both `open` and
 * `closed` results rather than being treated as closed.
 */
export function filterAttendanceEvents(
  events: readonly AttendanceEvent[],
  filters: AttendanceFilters,
): AttendanceEvent[] {
  // The fold is only needed to evaluate the employee-level session filter.
  const sessionStates =
    filters.sessionState === "all"
      ? null
      : deriveSessionStates(foldLatestEvents(events));

  return events.filter((event) => {
    if (
      filters.employeeId !== null &&
      event.employee_id !== filters.employeeId
    ) {
      return false;
    }

    if (
      filters.eventType !== "all" &&
      event.event_type !== filters.eventType
    ) {
      return false;
    }

    if (filters.utcDay !== null) {
      const instant = parseInstant(event.occurred_at);
      if (Number.isNaN(instant) || utcDayKey(instant) !== filters.utcDay) {
        return false;
      }
    }

    if (sessionStates !== null) {
      const state = sessionStates.get(event.employee_id);
      if (state !== filters.sessionState) {
        return false;
      }
    }

    return true;
  });
}
