import { EVENT_TYPE_CHECK_IN, EVENT_TYPE_CHECK_OUT } from "../api/types";
import type { AttendanceEvent } from "../api/types";
import {
  deriveSessionStates,
  foldLatestEvents,
  parseInstant,
  utcDayKey,
} from "./journal";

/** Counts for the Attendance summary, all derived from the journal itself. */
export interface JournalSummary {
  readonly totalEvents: number;
  readonly checkIns: number;
  readonly checkOuts: number;
  readonly undatedEvents: number;
  readonly unclassifiedEvents: number;
  readonly employeesWithEvents: number;
  readonly openSessions: number;
  readonly closedSessions: number;
}

/**
 * Summarise a journal snapshot.
 *
 * The session figures come from the shared fold (latest event per employee,
 * `(occurred_at, journal position)` ordering), so they agree with the Overview
 * and with the session column. Unknown types and unreadable timestamps are
 * counted but never become sessions.
 */
export function summariseJournal(
  events: readonly AttendanceEvent[],
): JournalSummary {
  const fold = foldLatestEvents(events);
  const states = deriveSessionStates(fold);

  let checkIns = 0;
  let checkOuts = 0;
  for (const event of events) {
    if (event.event_type === EVENT_TYPE_CHECK_IN) {
      checkIns += 1;
    } else if (event.event_type === EVENT_TYPE_CHECK_OUT) {
      checkOuts += 1;
    }
  }

  let openSessions = 0;
  for (const state of states.values()) {
    if (state === "open") {
      openSessions += 1;
    }
  }

  return {
    totalEvents: events.length,
    checkIns,
    checkOuts,
    undatedEvents: fold.undatedEvents,
    unclassifiedEvents: fold.unclassifiedEvents,
    employeesWithEvents: states.size,
    openSessions,
    closedSessions: states.size - openSessions,
  };
}

/**
 * Distinct UTC days present in the journal, newest first.
 *
 * Events with an unreadable timestamp are skipped: they belong to no day, which
 * is exactly why they stay visible only under "All dates".
 */
export function journalUtcDays(events: readonly AttendanceEvent[]): string[] {
  const days = new Set<string>();
  for (const event of events) {
    const instant = parseInstant(event.occurred_at);
    if (!Number.isNaN(instant)) {
      days.add(utcDayKey(instant));
    }
  }
  return Array.from(days).sort((left, right) => right.localeCompare(left));
}
