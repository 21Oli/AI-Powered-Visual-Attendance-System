import { describe, expect, it } from "vitest";

import type { AttendanceEvent } from "../src/lib/api/types";
import {
  ALL_ATTENDANCE_FILTERS,
  filterAttendanceEvents,
} from "../src/lib/attendance/filters";
import type { AttendanceFilters } from "../src/lib/attendance/filters";

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

/**
 * Journal covering every filter dimension:
 * A closes on the 11th, B is open on the 11th, C has only an unknown type,
 * D has an unreadable timestamp, E opens on the 12th, F closed on the 10th.
 */
const journal: readonly AttendanceEvent[] = [
  event("ev1", "A", "check_in", "2026-11-11T08:00:00Z"),
  event("ev2", "A", "check_out", "2026-11-11T12:00:00Z"),
  event("ev3", "B", "check_in", "2026-11-11T09:00:00Z"),
  event("ev4", "C", "lunch_break", "2026-11-11T10:00:00Z"),
  event("ev5", "D", "check_in", "not-a-date"),
  event("ev6", "E", "check_in", "2026-11-12T00:00:00Z"),
  event("ev7", "F", "check_out", "2026-11-10T23:59:59.999Z"),
];

function filters(overrides: Partial<AttendanceFilters>): AttendanceFilters {
  return { ...ALL_ATTENDANCE_FILTERS, ...overrides };
}

function ids(events: readonly AttendanceEvent[]): string[] {
  return events.map((entry) => entry.event_id);
}

describe("attendance filtering: date", () => {
  it("returns every event, in journal order, for all dates", () => {
    expect(ids(filterAttendanceEvents(journal, ALL_ATTENDANCE_FILTERS))).toEqual([
      "ev1",
      "ev2",
      "ev3",
      "ev4",
      "ev5",
      "ev6",
      "ev7",
    ]);
  });

  it("keeps only one UTC day", () => {
    expect(
      ids(filterAttendanceEvents(journal, filters({ utcDay: "2026-11-11" }))),
    ).toEqual(["ev1", "ev2", "ev3", "ev4"]);
  });

  it("treats UTC midnight as the day boundary", () => {
    const events = [
      event("at", "A", "check_in", "2026-11-11T23:59:59.999Z"),
      event("after", "A", "check_in", "2026-11-12T00:00:00Z"),
    ];

    expect(
      ids(filterAttendanceEvents(events, filters({ utcDay: "2026-11-11" }))),
    ).toEqual(["at"]);
    expect(
      ids(filterAttendanceEvents(events, filters({ utcDay: "2026-11-12" }))),
    ).toEqual(["after"]);
  });

  it("shows unreadable timestamps only when no date is selected", () => {
    expect(
      ids(filterAttendanceEvents(journal, filters({ utcDay: "2026-11-11" }))),
    ).not.toContain("ev5");
    expect(ids(filterAttendanceEvents(journal, ALL_ATTENDANCE_FILTERS))).toContain(
      "ev5",
    );
  });

  it("returns nothing for a day the journal does not contain", () => {
    expect(
      filterAttendanceEvents(journal, filters({ utcDay: "2026-01-01" })),
    ).toEqual([]);
  });
});

describe("attendance filtering: event type", () => {
  it("includes unknown types when all types are selected", () => {
    expect(ids(filterAttendanceEvents(journal, ALL_ATTENDANCE_FILTERS))).toContain(
      "ev4",
    );
  });

  it("keeps only check-ins", () => {
    expect(
      ids(filterAttendanceEvents(journal, filters({ eventType: "check_in" }))),
    ).toEqual(["ev1", "ev3", "ev5", "ev6"]);
  });

  it("keeps only check-outs", () => {
    expect(
      ids(filterAttendanceEvents(journal, filters({ eventType: "check_out" }))),
    ).toEqual(["ev2", "ev7"]);
  });
});

describe("attendance filtering: employee", () => {
  it("keeps one employee's events", () => {
    expect(
      ids(filterAttendanceEvents(journal, filters({ employeeId: "A" }))),
    ).toEqual(["ev1", "ev2"]);
  });

  it("returns nothing for an employee absent from the journal", () => {
    expect(
      filterAttendanceEvents(journal, filters({ employeeId: "Z" })),
    ).toEqual([]);
  });
});

describe("attendance filtering: session state", () => {
  it("keeps events whose employee currently has an open session", () => {
    expect(
      ids(filterAttendanceEvents(journal, filters({ sessionState: "open" }))),
    ).toEqual(["ev3", "ev6"]);
  });

  it("keeps events whose employee currently has a closed session", () => {
    expect(
      ids(filterAttendanceEvents(journal, filters({ sessionState: "closed" }))),
    ).toEqual(["ev1", "ev2", "ev7"]);
  });

  it("excludes employees without a valid session state from both filters", () => {
    const open = ids(filterAttendanceEvents(journal, filters({ sessionState: "open" })));
    const closed = ids(
      filterAttendanceEvents(journal, filters({ sessionState: "closed" })),
    );

    // C's only event is unclassified and D's timestamp is unreadable, so
    // neither is treated as closed (or open).
    expect(open).not.toContain("ev4");
    expect(open).not.toContain("ev5");
    expect(closed).not.toContain("ev4");
    expect(closed).not.toContain("ev5");
  });

  it("uses the latest event, not the first", () => {
    const reopening = [
      event("1", "A", "check_out", "2026-11-11T08:00:00Z"),
      event("2", "A", "check_in", "2026-11-11T09:00:00Z"),
    ];

    expect(
      ids(filterAttendanceEvents(reopening, filters({ sessionState: "open" }))),
    ).toEqual(["1", "2"]);
    expect(
      filterAttendanceEvents(reopening, filters({ sessionState: "closed" })),
    ).toEqual([]);
  });
});

describe("attendance filtering: composition and purity", () => {
  it("composes date, type, employee and session filters", () => {
    expect(
      ids(
        filterAttendanceEvents(
          journal,
          filters({
            utcDay: "2026-11-11",
            eventType: "check_in",
            employeeId: "B",
            sessionState: "open",
          }),
        ),
      ),
    ).toEqual(["ev3"]);
  });

  it("returns nothing when the filters cannot all hold", () => {
    expect(
      filterAttendanceEvents(
        journal,
        filters({ utcDay: "2026-11-11", employeeId: "E" }),
      ),
    ).toEqual([]);
  });

  it("preserves the original journal order rather than sorting", () => {
    const unsorted = [
      event("late", "A", "check_out", "2026-11-11T18:00:00Z"),
      event("early", "A", "check_in", "2026-11-11T06:00:00Z"),
      event("middle", "A", "lunch_break", "2026-11-11T12:00:00Z"),
    ];

    expect(
      ids(filterAttendanceEvents(unsorted, ALL_ATTENDANCE_FILTERS)),
    ).toEqual(["late", "early", "middle"]);
  });

  it("does not mutate the journal or its events", () => {
    const frozen = Object.freeze(
      journal.map((entry) => Object.freeze({ ...entry })),
    );
    const snapshot = JSON.stringify(frozen);

    filterAttendanceEvents(frozen, filters({ sessionState: "open" }));
    filterAttendanceEvents(frozen, filters({ utcDay: "2026-11-11" }));

    expect(JSON.stringify(frozen)).toBe(snapshot);
    expect(frozen).toHaveLength(7);
  });

  it("returns a new array rather than the source", () => {
    const result = filterAttendanceEvents(journal, ALL_ATTENDANCE_FILTERS);

    expect(result).not.toBe(journal);
    expect(result).toHaveLength(journal.length);
  });

  it("is deterministic", () => {
    const options = filters({ utcDay: "2026-11-11", sessionState: "closed" });

    expect(ids(filterAttendanceEvents(journal, options))).toEqual(
      ids(filterAttendanceEvents(journal, options)),
    );
  });
});
