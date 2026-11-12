import { describe, expect, it } from "vitest";

import type { AttendanceEvent } from "../src/lib/api/types";
import {
  ATTENDANCE_PAGE_SIZES,
  DEFAULT_ATTENDANCE_PAGE_SIZE,
  normaliseAttendancePageSize,
  paginateAttendanceEvents,
} from "../src/lib/attendance/pagination";

const event = (id: string): AttendanceEvent => ({
  event_id: id,
  employee_id: "A",
  event_type: "check_in",
  occurred_at: "2026-11-11T08:00:00Z",
});

/** A journal of `count` events with ids "1"…"count", in journal order. */
function journal(count: number): AttendanceEvent[] {
  return Array.from({ length: count }, (_, index) => event(String(index + 1)));
}

function ids(page: { items: readonly AttendanceEvent[] }): string[] {
  return page.items.map((entry) => entry.event_id);
}

describe("attendance pagination: empty and single page", () => {
  it("handles an empty list", () => {
    const page = paginateAttendanceEvents([]);

    expect(page.items).toEqual([]);
    expect(page.page).toBe(1);
    expect(page.pageSize).toBe(20);
    expect(page.totalItems).toBe(0);
    expect(page.totalPages).toBe(1);
    expect(page.hasPrevious).toBe(false);
    expect(page.hasNext).toBe(false);
  });

  it("returns one page when everything fits", () => {
    const page = paginateAttendanceEvents(journal(5));

    expect(ids(page)).toEqual(["1", "2", "3", "4", "5"]);
    expect(page.totalItems).toBe(5);
    expect(page.totalPages).toBe(1);
    expect(page.hasPrevious).toBe(false);
    expect(page.hasNext).toBe(false);
  });

  it("fills a page exactly at its boundary without inventing an extra page", () => {
    const page = paginateAttendanceEvents(journal(20));

    expect(page.items).toHaveLength(20);
    expect(page.totalPages).toBe(1);
  });
});

describe("attendance pagination: multiple pages", () => {
  it("splits 45 events into three pages of 20", () => {
    const events = journal(45);

    expect(ids(paginateAttendanceEvents(events, 1))).toEqual(
      Array.from({ length: 20 }, (_, index) => String(index + 1)),
    );
    expect(ids(paginateAttendanceEvents(events, 2))).toEqual(
      Array.from({ length: 20 }, (_, index) => String(index + 21)),
    );
    expect(ids(paginateAttendanceEvents(events, 3))).toEqual([
      "41",
      "42",
      "43",
      "44",
      "45",
    ]);
  });

  it("reports the first page flags", () => {
    const page = paginateAttendanceEvents(journal(45), 1);

    expect(page.page).toBe(1);
    expect(page.totalPages).toBe(3);
    expect(page.hasPrevious).toBe(false);
    expect(page.hasNext).toBe(true);
  });

  it("reports the last page flags", () => {
    const page = paginateAttendanceEvents(journal(45), 3);

    expect(page.page).toBe(3);
    expect(page.hasPrevious).toBe(true);
    expect(page.hasNext).toBe(false);
  });

  it("clamps a page beyond the last page instead of returning an empty one", () => {
    const page = paginateAttendanceEvents(journal(45), 99, 20);

    expect(page.page).toBe(3);
    expect(page.items).toHaveLength(5);
    expect(page.hasNext).toBe(false);
    expect(page.hasPrevious).toBe(true);
  });

  it("treats zero and negative pages as page one", () => {
    const events = journal(45);

    expect(paginateAttendanceEvents(events, 0).page).toBe(1);
    expect(paginateAttendanceEvents(events, -5).page).toBe(1);
    expect(ids(paginateAttendanceEvents(events, 0))).toEqual(
      ids(paginateAttendanceEvents(events, 1)),
    );
  });

  it("treats a non-finite page as page one", () => {
    expect(paginateAttendanceEvents(journal(45), Number.NaN).page).toBe(1);
  });
});

describe("attendance pagination: page size", () => {
  it("defaults to 20", () => {
    expect(DEFAULT_ATTENDANCE_PAGE_SIZE).toBe(20);
    expect(paginateAttendanceEvents(journal(25)).pageSize).toBe(20);
    expect(paginateAttendanceEvents(journal(25)).totalPages).toBe(2);
  });

  it("supports exactly 10, 20, 50 and 100", () => {
    expect([...ATTENDANCE_PAGE_SIZES]).toEqual([10, 20, 50, 100]);

    for (const size of ATTENDANCE_PAGE_SIZES) {
      const page = paginateAttendanceEvents(journal(100), 1, size);
      expect(page.pageSize).toBe(size);
      expect(page.items).toHaveLength(size);
      expect(page.totalPages).toBe(100 / size);
    }
  });

  it("falls back to the default for an unsupported size", () => {
    expect(normaliseAttendancePageSize(7)).toBe(20);
    expect(normaliseAttendancePageSize(0)).toBe(20);
    expect(normaliseAttendancePageSize(-10)).toBe(20);
    expect(normaliseAttendancePageSize(Number.NaN)).toBe(20);
    expect(normaliseAttendancePageSize(null)).toBe(20);
    expect(normaliseAttendancePageSize(undefined)).toBe(20);
    expect(normaliseAttendancePageSize(50)).toBe(50);
  });

  it("keeps the requested page when the size changes", () => {
    const page = paginateAttendanceEvents(journal(100), 2, 50);

    expect(page.page).toBe(2);
    expect(ids(page)).toEqual(Array.from({ length: 50 }, (_, index) => String(index + 51)));
  });
});

describe("attendance pagination: purity", () => {
  it("does not mutate or reorder the source list", () => {
    const events = Object.freeze(journal(45));
    const snapshot = JSON.stringify(events);

    paginateAttendanceEvents(events, 2);
    paginateAttendanceEvents(events, 3, 10);

    expect(JSON.stringify(events)).toBe(snapshot);
    expect(events).toHaveLength(45);
  });

  it("is deterministic", () => {
    const events = journal(45);

    expect(ids(paginateAttendanceEvents(events, 2))).toEqual(
      ids(paginateAttendanceEvents(events, 2)),
    );
  });

  it("keeps totalItems consistent across pages", () => {
    const events = journal(45);

    for (const page of [1, 2, 3]) {
      const result = paginateAttendanceEvents(events, page);
      expect(result.totalItems).toBe(45);
      expect(result.totalPages).toBe(3);
    }
  });
});
