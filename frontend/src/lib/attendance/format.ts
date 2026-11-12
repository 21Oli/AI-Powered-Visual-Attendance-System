import { EVENT_TYPE_CHECK_IN, EVENT_TYPE_CHECK_OUT } from "../api/types";
import type { SessionState } from "./journal";

/**
 * Display formatting for attendance records.
 *
 * Everything is rendered in **UTC** and without locale-dependent formatting, so
 * the same journal looks the same on every machine. An unreadable timestamp is
 * reported as unavailable — the value is never coerced into a plausible date.
 */

export const UNAVAILABLE_TIMESTAMP = "Unavailable";
export const UNKNOWN_EMPLOYEE_LABEL = "Unknown employee";
export const EMPLOYEE_DETAILS_UNAVAILABLE = "Employee details unavailable";

/** `YYYY-MM-DD HH:MM:SS UTC`, or an explicit unavailable label. */
export function formatOccurredAt(value: string): string {
  const instant = Date.parse(value);
  if (Number.isNaN(instant)) {
    return UNAVAILABLE_TIMESTAMP;
  }
  const iso = new Date(instant).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`;
}

export interface EventTypeLabel {
  readonly label: string;
  /** `false` for a type this dashboard does not recognise. */
  readonly known: boolean;
}

/** Human label for one event type, keeping unknown values visible. */
export function formatEventType(value: string): EventTypeLabel {
  if (value === EVENT_TYPE_CHECK_IN) {
    return { label: "Check-in", known: true };
  }
  if (value === EVENT_TYPE_CHECK_OUT) {
    return { label: "Check-out", known: true };
  }
  return { label: `Unknown type (${value})`, known: false };
}

/** Label for the employee's derived current session state. */
export function formatSessionState(state: SessionState | undefined): string {
  if (state === "open") {
    return "Open";
  }
  if (state === "closed") {
    return "Closed";
  }
  return "Unknown";
}

/** UTC day option label for the date filter. */
export function formatUtcDayOption(day: string): string {
  return `${day} (UTC)`;
}
