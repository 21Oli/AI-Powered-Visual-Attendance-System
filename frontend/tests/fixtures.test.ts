import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createOverviewSource, resolveDataSourceKind } from "../src/lib/api/data-source";
import {
  FIXTURE_LABEL,
  fixtureAttendance,
  fixtureEmployees,
  isFixtureModeEnabled,
} from "../src/lib/fixtures/overview-fixtures";
import { parseAttendance, parseEmployee, parseEmployeeList } from "../src/lib/api/types";

describe("fixture gating", () => {
  it("is off by default", () => {
    expect(resolveDataSourceKind({})).toBe("live");
    expect(isFixtureModeEnabled({})).toBe(false);
  });

  it("requires a development build and an explicit opt-in", () => {
    expect(
      resolveDataSourceKind({ NODE_ENV: "development", DASHBOARD_DATA_MODE: "fixtures" }),
    ).toBe("fixture");
    expect(
      isFixtureModeEnabled({ NODE_ENV: "development", DASHBOARD_DATA_MODE: " Fixtures " }),
    ).toBe(true);
  });

  it("cannot be enabled in a production build", () => {
    expect(
      resolveDataSourceKind({
        NODE_ENV: "production",
        DASHBOARD_DATA_MODE: "fixtures",
      }),
    ).toBe("live");
    expect(
      resolveDataSourceKind({ NODE_ENV: undefined, DASHBOARD_DATA_MODE: "fixtures" }),
    ).toBe("live");
  });

  it("requires the exact opt-in value", () => {
    for (const mode of ["", "fixture", "true", "1", "mock"]) {
      expect(
        resolveDataSourceKind({ NODE_ENV: "development", DASHBOARD_DATA_MODE: mode }),
      ).toBe("live");
    }
  });
});

describe("fixture data", () => {
  it("is labelled unmistakably", () => {
    expect(FIXTURE_LABEL).toContain("FIXTURE DATA");
    expect(FIXTURE_LABEL).toContain("DEVELOPMENT ONLY");
  });

  it("matches the employee contract exactly", () => {
    const employees = fixtureEmployees();
    expect(employees.length).toBeGreaterThan(0);
    for (const employee of employees) {
      expect(parseEmployee(employee)).not.toBeNull();
      expect(employee.full_name).toMatch(/fixture/i);
    }
    expect(parseEmployeeList({ employees, count: employees.length, limit: 100, next_after: null })).not.toBeNull();
  });

  it("matches the attendance contract exactly, including nullable timestamps", () => {
    const attendance = fixtureAttendance(Date.parse("2026-11-11T12:00:00Z"));
    expect(parseAttendance(attendance)).not.toBeNull();
    expect(attendance.events.length).toBe(attendance.count);
    expect(attendance.incomplete_trailing_record).toBe(true);
    // A nullable created_at is part of the real contract, so the fixture covers it.
    expect(fixtureEmployees().some((employee) => employee.created_at === null)).toBe(true);
  });

  it("is deterministic for a given clock", () => {
    const first = fixtureAttendance(1_000_000);
    const second = fixtureAttendance(1_000_000);
    expect(first).toEqual(second);
    expect(fixtureAttendance(2_000_000)).not.toEqual(first);
  });

  it("contains no real employee identifiers", () => {
    for (const employee of fixtureEmployees()) {
      expect(employee.employee_id.startsWith("FIX-")).toBe(true);
    }
  });
});

describe("data sources", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("exposes the fixture kind so the UI can label it", () => {
    const source = createOverviewSource("fixture", () => 1_000_000);
    expect(source.kind).toBe("fixture");
  });

  it("serves fixture data without touching the network", async () => {
    const source = createOverviewSource("fixture", () => Date.parse("2026-11-11T09:00:00Z"));
    const [employees, attendance] = await Promise.all([
      source.loadEmployees(),
      source.loadAttendance(),
    ]);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(employees).toHaveLength(fixtureEmployees().length);
    expect(attendance.incomplete_trailing_record).toBe(true);
  });

  it("never silently replaces a failed live request with fixtures", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const source = createOverviewSource("live");

    expect(source.kind).toBe("live");
    await expect(source.loadEmployees()).rejects.toMatchObject({
      kind: "network",
    });
    expect(fetchMock).toHaveBeenCalled();
  });

  it("does not read fixtures from a live source on success", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ employees: [], count: 0, limit: 100, next_after: null }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    const source = createOverviewSource("live");
    await expect(source.loadEmployees()).resolves.toEqual([]);
  });
});
