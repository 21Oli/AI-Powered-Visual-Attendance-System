import { fetchAttendance } from "./attendance";
import { fetchAllEmployees } from "./employees";
import { isFixtureModeEnabled } from "../fixtures/overview-fixtures";
import { fixtureAttendance, fixtureEmployees } from "../fixtures/overview-fixtures";
import type { AttendanceResponse, Employee } from "./types";

export type DataSourceKind = "live" | "fixture";

export interface OverviewSource {
  readonly kind: DataSourceKind;
  loadEmployees(signal?: AbortSignal): Promise<Employee[]>;
  loadAttendance(signal?: AbortSignal): Promise<AttendanceResponse>;
}

/**
 * Resolve the source on the **server** and pass the result down.
 *
 * Fixture mode therefore cannot be switched on by the browser, and a production
 * build ignores `DASHBOARD_DATA_MODE` entirely.
 */
export function resolveDataSourceKind(env: {
  readonly NODE_ENV?: string;
  readonly DASHBOARD_DATA_MODE?: string;
}): DataSourceKind {
  return isFixtureModeEnabled(env) ? "fixture" : "live";
}

function assertLive(signal?: AbortSignal): void {
  signal?.throwIfAborted();
}

function createLiveSource(): OverviewSource {
  return {
    kind: "live",
    loadEmployees: (signal) => {
      assertLive(signal);
      return fetchAllEmployees(signal);
    },
    loadAttendance: (signal) => {
      assertLive(signal);
      return fetchAttendance(signal);
    },
  };
}

function createFixtureSource(now: () => number): OverviewSource {
  return {
    kind: "fixture",
    loadEmployees: async (signal) => {
      assertLive(signal);
      return fixtureEmployees();
    },
    loadAttendance: async (signal) => {
      assertLive(signal);
      return fixtureAttendance(now());
    },
  };
}

/**
 * Build the source for a page render.
 *
 * There is deliberately **no** try/catch that could turn a failed live request
 * into fixture data: a failing backend produces an error state, never fake
 * numbers.
 */
export function createOverviewSource(
  kind: DataSourceKind,
  now: () => number = Date.now,
): OverviewSource {
  return kind === "fixture" ? createFixtureSource(now) : createLiveSource();
}
