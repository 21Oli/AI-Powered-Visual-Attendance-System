import { describe, expect, it } from "vitest";

import type { AttendanceEvent } from "../src/lib/api/types";
import {
  deriveSessionStates,
  foldLatestEvents,
  parseInstant,
  utcDayKey,
} from "../src/lib/attendance/journal";
import { summariseSessions } from "../src/lib/dashboard/overview";

const event = (
  id: string,
  employeeId: string,
  type: string,
  occurredAt: string,
): AttendanceEvent => ({
  event_id: id,
  employee_id: employeeId,
  event_type: type,
  occurred_at: occurredAt,
});

/** Compact view of the fold, for deterministic ordering assertions. */
function foldedEntries(events: readonly AttendanceEvent[]) {
  const fold = foldLatestEvents(events);
  return Array.from(fold.latest.entries()).map(([employeeId, boundary]) => ({
    employeeId,
    eventId: boundary.event.event_id,
    instant: boundary.instant,
    position: boundary.position,
  }));
}

describe("journal fold: latest session boundary", () => {
  it("keeps a single check-in as an open boundary", () => {
    const fold = foldLatestEvents([
      event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
    ]);

    expect(fold.latest.size).toBe(1);
    expect(fold.latest.get("A")?.event.event_id).toBe("1");
    expect(fold.latest.get("A")?.position).toBe(0);
    expect(deriveSessionStates(fold).get("A")).toBe("open");
    expect(fold.undatedEvents).toBe(0);
    expect(fold.unclassifiedEvents).toBe(0);
  });

  it("closes the session when a later check-out follows", () => {
    const fold = foldLatestEvents([
      event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
      event("2", "A", "check_out", "2026-11-11T12:00:00Z"),
    ]);

    expect(fold.latest.get("A")?.event.event_id).toBe("2");
    expect(deriveSessionStates(fold).get("A")).toBe("closed");
  });

  it("reopens the session when a later check-in follows a check-out", () => {
    const fold = foldLatestEvents([
      event("1", "A", "check_out", "2026-11-11T08:00:00Z"),
      event("2", "A", "check_in", "2026-11-11T09:00:00Z"),
    ]);

    expect(fold.latest.get("A")?.event.event_id).toBe("2");
    expect(deriveSessionStates(fold).get("A")).toBe("open");
  });

  it("orders by timestamp even when journal order is not chronological", () => {
    // Journal (append) order puts the later timestamp first on purpose.
    const fold = foldLatestEvents([
      event("1", "A", "check_in", "2026-11-11T12:00:00Z"),
      event("2", "A", "check_out", "2026-11-11T08:00:00Z"),
    ]);

    expect(fold.latest.get("A")?.event.event_id).toBe("1");
    expect(deriveSessionStates(fold).get("A")).toBe("open");
  });

  it("resolves an exact timestamp tie by the later journal position", () => {
    const sameInstant = "2026-11-11T08:00:00Z";

    const checkOutLast = foldLatestEvents([
      event("1", "A", "check_in", sameInstant),
      event("2", "A", "check_out", sameInstant),
    ]);
    expect(checkOutLast.latest.get("A")?.event.event_id).toBe("2");
    expect(deriveSessionStates(checkOutLast).get("A")).toBe("closed");

    const checkInLast = foldLatestEvents([
      event("1", "A", "check_out", sameInstant),
      event("2", "A", "check_in", sameInstant),
    ]);
    expect(checkInLast.latest.get("A")?.event.event_id).toBe("2");
    expect(deriveSessionStates(checkInLast).get("A")).toBe("open");
  });

  it("tracks employees independently, in first-seen journal order", () => {
    const entries = foldedEntries([
      event("1", "B", "check_in", "2026-11-11T08:00:00Z"),
      event("2", "A", "check_in", "2026-11-11T08:30:00Z"),
      event("3", "B", "check_out", "2026-11-11T09:00:00Z"),
    ]);

    expect(entries.map((entry) => entry.employeeId)).toEqual(["B", "A"]);
    expect(entries.map((entry) => entry.eventId)).toEqual(["3", "2"]);
  });

  it("excludes an unreadable timestamp and counts it as undated", () => {
    const fold = foldLatestEvents([
      event("1", "A", "check_in", "not-a-date"),
      event("2", "A", "check_in", "2026-11-11T08:00:00Z"),
    ]);

    expect(fold.undatedEvents).toBe(1);
    expect(fold.latest.get("A")?.event.event_id).toBe("2");
  });

  it("excludes an empty timestamp and keeps the employee stateless", () => {
    const fold = foldLatestEvents([event("1", "A", "check_in", "")]);

    expect(fold.undatedEvents).toBe(1);
    expect(fold.latest.size).toBe(0);
    expect(deriveSessionStates(fold).has("A")).toBe(false);
  });

  it("counts an event with both an unreadable timestamp and an unknown type as undated", () => {
    const fold = foldLatestEvents([event("1", "A", "lunch_break", "garbage")]);

    expect(fold.undatedEvents).toBe(1);
    expect(fold.unclassifiedEvents).toBe(0);
    expect(fold.latest.size).toBe(0);
  });

  it("excludes an unknown event type without inventing a session", () => {
    const fold = foldLatestEvents([
      event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
      event("2", "A", "lunch_break", "2026-11-11T09:00:00Z"),
    ]);

    expect(fold.unclassifiedEvents).toBe(1);
    expect(fold.latest.get("A")?.event.event_id).toBe("1");
    expect(deriveSessionStates(fold).get("A")).toBe("open");
  });

  it("leaves an employee with only unknown events without a state", () => {
    const fold = foldLatestEvents([
      event("1", "A", "lunch_break", "2026-11-11T09:00:00Z"),
    ]);

    expect(fold.unclassifiedEvents).toBe(1);
    expect(deriveSessionStates(fold).size).toBe(0);
  });

  it("returns an empty fold for an empty journal", () => {
    const fold = foldLatestEvents([]);

    expect(fold.latest.size).toBe(0);
    expect(fold.undatedEvents).toBe(0);
    expect(fold.unclassifiedEvents).toBe(0);
    expect(deriveSessionStates(fold).size).toBe(0);
  });

  it("does not mutate the journal or its events", () => {
    const events = Object.freeze([
      Object.freeze(event("1", "A", "check_in", "2026-11-11T08:00:00Z")),
      Object.freeze(event("2", "A", "check_out", "2026-11-11T12:00:00Z")),
      Object.freeze(event("3", "B", "lunch_break", "2026-11-11T13:00:00Z")),
    ]);
    const snapshot = JSON.stringify(events);

    const fold = foldLatestEvents(events);
    deriveSessionStates(fold);

    expect(JSON.stringify(events)).toBe(snapshot);
    expect(events).toHaveLength(3);
  });

  it("is deterministic for the same input", () => {
    const events = [
      event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
      event("2", "B", "check_in", "2026-11-11T08:00:00Z"),
      event("3", "A", "check_out", "2026-11-11T08:00:00Z"),
    ];

    expect(foldedEntries(events)).toEqual(foldedEntries(events));
  });
});

describe("shared instant and UTC day helpers", () => {
  it("parses instants and reports unreadable values as NaN", () => {
    expect(parseInstant("2026-11-11T00:00:00Z")).toBe(
      Date.parse("2026-11-11T00:00:00Z"),
    );
    expect(Number.isNaN(parseInstant("not-a-date"))).toBe(true);
  });

  it("keys days in UTC", () => {
    expect(utcDayKey(Date.parse("2026-11-11T00:00:00Z"))).toBe("2026-11-11");
    expect(utcDayKey(Date.parse("2026-11-11T23:59:59.999Z"))).toBe("2026-11-11");
    expect(utcDayKey(Date.parse("2026-11-12T00:00:00Z"))).toBe("2026-11-12");
  });
});

describe("overview session summary compatibility", () => {
  it("keeps the existing result shape", () => {
    const summary = summariseSessions([]);

    expect(Object.keys(summary).sort()).toEqual([
      "employeesWithEvents",
      "openSessions",
      "unclassifiedEvents",
      "undatedEvents",
    ]);
  });

  it("returns the same figures as before for a mixed journal", () => {
    const summary = summariseSessions([
      event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
      event("2", "A", "check_out", "2026-11-11T12:00:00Z"),
      event("3", "A", "check_in", "2026-11-11T13:00:00Z"),
      event("4", "B", "check_in", "2026-11-11T08:30:00Z"),
      event("5", "B", "check_out", "2026-11-11T09:00:00Z"),
      event("6", "C", "lunch_break", "2026-11-11T10:00:00Z"),
      event("7", "D", "check_in", "garbage"),
    ]);

    expect(summary).toEqual({
      openSessions: 1,
      employeesWithEvents: 2,
      undatedEvents: 1,
      unclassifiedEvents: 1,
    });
  });

  it("still resolves ties by journal position", () => {
    const sameInstant = "2026-11-11T08:00:00Z";

    expect(
      summariseSessions([
        event("1", "A", "check_in", sameInstant),
        event("2", "A", "check_out", sameInstant),
      ]).openSessions,
    ).toBe(0);
    expect(
      summariseSessions([
        event("1", "A", "check_out", sameInstant),
        event("2", "A", "check_in", sameInstant),
      ]).openSessions,
    ).toBe(1);
  });

  it("agrees with the shared fold for open sessions", () => {
    const events = [
      event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
      event("2", "B", "check_out", "2026-11-11T09:00:00Z"),
      event("3", "C", "check_in", "2026-11-11T10:00:00Z"),
      event("4", "C", "check_in", "2026-11-11T11:00:00Z"),
    ];
    const states = deriveSessionStates(foldLatestEvents(events));
    const openFromFold = Array.from(states.values()).filter(
      (state) => state === "open",
    ).length;

    expect(summariseSessions(events).openSessions).toBe(openFromFold);
    expect(summariseSessions(events).employeesWithEvents).toBe(states.size);
  });
});
