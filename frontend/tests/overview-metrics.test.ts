import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchAllEmployees, MAX_EMPLOYEE_PAGES } from "../src/lib/api/employees";
import type { AttendanceEvent, Employee } from "../src/lib/api/types";
import {
  buildOverviewMetrics,
  countEventsOnUtcDay,
  parseInstant,
  summariseEmployees,
  summariseSessions,
  utcDayKey,
} from "../src/lib/dashboard/overview";

const fetchMock = vi.fn<typeof fetch>();

function page(ids: string[], nextAfter: string | null): Response {
  const employees = ids.map((id) => ({
    employee_id: id,
    full_name: `Employee ${id}`,
    is_active: true,
    created_at: null,
  }));
  return new Response(
    JSON.stringify({ employees, count: employees.length, limit: 100, next_after: nextAfter }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const employee = (id: string, isActive: boolean): Employee => ({
  employee_id: id,
  full_name: `Employee ${id}`,
  is_active: isActive,
  created_at: null,
});

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

describe("employee totals", () => {
  it("counts total, active and inactive from is_active only", () => {
    const totals = summariseEmployees([
      employee("A", true),
      employee("B", false),
      employee("C", true),
    ]);
    expect(totals).toEqual({ total: 3, active: 2, inactive: 1 });
  });

  it("reports zero for an empty register", () => {
    expect(summariseEmployees([])).toEqual({ total: 0, active: 0, inactive: 0 });
  });
});

describe("employee pagination", () => {
  it("walks every keyset page and counts the full collection", async () => {
    fetchMock
      .mockResolvedValueOnce(page(["A", "B"], "B"))
      .mockResolvedValueOnce(page(["C"], "C"))
      .mockResolvedValueOnce(page(["D"], null));

    const employees = await fetchAllEmployees();

    expect(employees.map((entry) => entry.employee_id)).toEqual(["A", "B", "C", "D"]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "/api/backend/api/v1/employees?limit=100&after=B",
    );
    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      "/api/backend/api/v1/employees?limit=100&after=C",
    );
  });

  it("asks for the maximum page size", async () => {
    fetchMock.mockResolvedValueOnce(page([], null));
    await fetchAllEmployees();
    expect(fetchMock.mock.calls[0]?.[0]).toContain("limit=100");
  });

  it("stops at the first short final page", async () => {
    fetchMock.mockResolvedValueOnce(page(["A"], null));
    await fetchAllEmployees();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("rejects a repeated cursor instead of looping", async () => {
    fetchMock
      .mockResolvedValueOnce(page(["A"], "A"))
      .mockResolvedValueOnce(page(["B"], "A"));

    await expect(fetchAllEmployees()).rejects.toMatchObject({
      code: "PAGINATION_CURSOR_REPEATED",
    });
  });

  it("propagates a failed later page rather than returning a partial total", async () => {
    fetchMock
      .mockResolvedValueOnce(page(["A"], "A"))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"));

    await expect(fetchAllEmployees()).rejects.toMatchObject({
      kind: "network",
    });
  });

  it("stops a pathological walk", async () => {
    let counter = 0;
    fetchMock.mockImplementation(() => {
      counter += 1;
      return Promise.resolve(page([`E${counter}`], `E${counter}`));
    });

    await expect(fetchAllEmployees()).rejects.toMatchObject({
      code: "PAGINATION_LIMIT_REACHED",
    });
    expect(fetchMock).toHaveBeenCalledTimes(MAX_EMPLOYEE_PAGES);
  });
});

describe("UTC day boundaries", () => {
  it("keys days in UTC", () => {
    expect(utcDayKey(Date.parse("2026-11-11T00:00:00.000Z"))).toBe("2026-11-11");
    expect(utcDayKey(Date.parse("2026-11-11T23:59:59.999Z"))).toBe("2026-11-11");
    expect(utcDayKey(Date.parse("2026-11-12T00:00:00.000Z"))).toBe("2026-11-12");
  });

  it("counts events from exactly midnight to exactly the next midnight", () => {
    const now = Date.parse("2026-11-11T23:30:00Z");
    const events = [
      event("1", "A", "check_in", "2026-11-11T00:00:00Z"),
      event("2", "A", "check_out", "2026-11-11T00:00:00+00:00"),
      event("3", "B", "check_in", "2026-11-11T23:59:59.999Z"),
      event("4", "B", "check_in", "2026-11-12T00:00:00Z"),
      event("5", "C", "check_in", "2026-11-10T23:59:59Z"),
    ];

    expect(countEventsOnUtcDay(events, now)).toBe(3);
  });

  it("ignores unreadable timestamps", () => {
    expect(countEventsOnUtcDay([event("1", "A", "check_in", "not-a-date")], Date.parse("2026-11-11T00:00:00Z"))).toBe(0);
    expect(Number.isNaN(parseInstant("not-a-date"))).toBe(true);
  });
});

describe("session folding", () => {
  it("counts an employee once, using their latest event", () => {
    const summary = summariseSessions([
      event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
      event("2", "A", "check_out", "2026-11-11T12:00:00Z"),
      event("3", "A", "check_in", "2026-11-11T13:00:00Z"),
      event("4", "B", "check_in", "2026-11-11T08:30:00Z"),
      event("5", "B", "check_out", "2026-11-11T09:00:00Z"),
    ]);

    expect(summary.openSessions).toBe(1);
    expect(summary.employeesWithEvents).toBe(2);
  });

  it("resolves an exact tie by journal position", () => {
    const sameInstant = "2026-11-11T08:00:00Z";
    const closedLast = summariseSessions([
      event("1", "A", "check_in", sameInstant),
      event("2", "A", "check_out", sameInstant),
    ]);
    const openLast = summariseSessions([
      event("1", "A", "check_out", sameInstant),
      event("2", "A", "check_in", sameInstant),
    ]);

    expect(closedLast.openSessions).toBe(0);
    expect(openLast.openSessions).toBe(1);
  });

  it("excludes undated and unclassified events without inventing a session", () => {
    const summary = summariseSessions([
      event("1", "A", "check_in", "garbage"),
      event("2", "B", "lunch_break", "2026-11-11T08:00:00Z"),
      event("3", "C", "check_in", "2026-11-11T08:00:00Z"),
    ]);

    expect(summary.openSessions).toBe(1);
    expect(summary.undatedEvents).toBe(1);
    expect(summary.unclassifiedEvents).toBe(1);
  });

  it("returns zero for an empty journal", () => {
    expect(summariseSessions([])).toEqual({
      openSessions: 0,
      employeesWithEvents: 0,
      undatedEvents: 0,
      unclassifiedEvents: 0,
    });
  });
});

describe("combined metrics", () => {
  it("derives every figure from the two responses", () => {
    const metrics = buildOverviewMetrics(
      [employee("A", true), employee("B", false)],
      {
        events: [
          event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
          event("2", "B", "check_in", "2026-11-11T07:00:00Z"),
          event("3", "B", "check_out", "2026-11-11T09:00:00Z"),
        ],
        incomplete_trailing_record: false,
      },
      Date.parse("2026-11-11T10:00:00Z"),
    );

    expect(metrics.employeeTotals).toEqual({ total: 2, active: 1, inactive: 1 });
    expect(metrics.eventsToday).toBe(3);
    expect(metrics.sessions.openSessions).toBe(1);
    expect(metrics.utcDay).toBe("2026-11-11");
    expect(metrics.incompleteTrailingRecord).toBe(false);
  });

  it("surfaces the incomplete trailing record flag unchanged", () => {
    const metrics = buildOverviewMetrics([], {
      events: [],
      incomplete_trailing_record: true,
    }, 0);
    expect(metrics.incompleteTrailingRecord).toBe(true);
  });
});
