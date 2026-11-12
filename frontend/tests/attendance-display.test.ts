import { describe, expect, it } from "vitest";

import type { AttendanceEvent } from "../src/lib/api/types";
import {
  formatEventType,
  formatOccurredAt,
  formatSessionState,
  formatUtcDayOption,
} from "../src/lib/attendance/format";
import {
  journalUtcDays,
  summariseJournal,
} from "../src/lib/attendance/summary";

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

describe("attendance timestamp formatting", () => {
  it("renders an ISO timestamp in UTC with an explicit suffix", () => {
    expect(formatOccurredAt("2026-11-11T18:05:09+00:00")).toBe(
      "2026-11-11 18:05:09 UTC",
    );
    expect(formatOccurredAt("2026-11-11T18:05:09Z")).toBe(
      "2026-11-11 18:05:09 UTC",
    );
  });

  it("normalises a non-UTC offset to UTC", () => {
    expect(formatOccurredAt("2026-11-11T20:05:09+02:00")).toBe(
      "2026-11-11 18:05:09 UTC",
    );
  });

  it("never fabricates a date for an unreadable timestamp", () => {
    expect(formatOccurredAt("not-a-date")).toBe("Unavailable");
    expect(formatOccurredAt("")).toBe("Unavailable");
  });
});

describe("attendance labels", () => {
  it("labels the two known event types", () => {
    expect(formatEventType("check_in")).toEqual({
      label: "Check-in",
      known: true,
    });
    expect(formatEventType("check_out")).toEqual({
      label: "Check-out",
      known: true,
    });
  });

  it("keeps an unknown event type visible and unsupported", () => {
    expect(formatEventType("lunch_break")).toEqual({
      label: "Unknown type (lunch_break)",
      known: false,
    });
  });

  it("labels session state and never guesses", () => {
    expect(formatSessionState("open")).toBe("Open");
    expect(formatSessionState("closed")).toBe("Closed");
    expect(formatSessionState(undefined)).toBe("Unknown");
  });

  it("labels date-filter options as UTC", () => {
    expect(formatUtcDayOption("2026-11-11")).toBe("2026-11-11 (UTC)");
  });
});

describe("journal summary", () => {
  const events = [
    event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
    event("2", "A", "check_out", "2026-11-11T12:00:00Z"),
    event("3", "B", "check_in", "2026-11-11T09:00:00Z"),
    event("4", "C", "lunch_break", "2026-11-11T10:00:00Z"),
    event("5", "D", "check_in", "not-a-date"),
  ];

  it("counts events, types and diagnostics", () => {
    const summary = summariseJournal(events);

    expect(summary.totalEvents).toBe(5);
    expect(summary.checkIns).toBe(3);
    expect(summary.checkOuts).toBe(1);
    expect(summary.unclassifiedEvents).toBe(1);
    expect(summary.undatedEvents).toBe(1);
  });

  it("derives sessions only from valid boundaries", () => {
    const summary = summariseJournal(events);

    // A is closed, B is open; C's type and D's timestamp are not sessions.
    expect(summary.employeesWithEvents).toBe(2);
    expect(summary.openSessions).toBe(1);
    expect(summary.closedSessions).toBe(1);
  });

  it("returns zeroes for an empty journal", () => {
    expect(summariseJournal([])).toEqual({
      totalEvents: 0,
      checkIns: 0,
      checkOuts: 0,
      undatedEvents: 0,
      unclassifiedEvents: 0,
      employeesWithEvents: 0,
      openSessions: 0,
      closedSessions: 0,
    });
  });
});

describe("journal UTC days", () => {
  it("lists distinct days, newest first, skipping unreadable timestamps", () => {
    const days = journalUtcDays([
      event("1", "A", "check_in", "2026-11-10T23:59:59Z"),
      event("2", "A", "check_in", "2026-11-11T00:00:00Z"),
      event("3", "A", "check_in", "2026-11-12T12:00:00Z"),
      event("4", "A", "check_in", "2026-11-11T23:59:59.999Z"),
      event("5", "A", "check_in", "not-a-date"),
    ]);

    expect(days).toEqual(["2026-11-12", "2026-11-11", "2026-11-10"]);
  });

  it("returns no days for a journal of unreadable timestamps", () => {
    expect(journalUtcDays([event("1", "A", "check_in", "garbage")])).toEqual([]);
  });
});
