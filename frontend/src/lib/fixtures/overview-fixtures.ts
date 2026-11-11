import { EVENT_TYPE_CHECK_IN, EVENT_TYPE_CHECK_OUT } from "../api/types";
import type { AttendanceResponse, Employee } from "../api/types";

/**
 * Synthetic, development-only fixture data.
 *
 * Rules this module exists to honour:
 * * every value is invented and plainly labelled — no real employee data;
 * * the shapes are exactly the backend contract, so the same parsers accept them;
 * * generation is deterministic for a given clock (tests pass a fixed `now`);
 * * nothing here is reachable unless fixture mode is explicitly resolved on the
 *   server in a development build.
 */

export const FIXTURE_LABEL = "FIXTURE DATA — DEVELOPMENT ONLY";

export const FIXTURE_SOURCE_KIND = "fixture" as const;

/** Six synthetic employees, mixed active/inactive, with stable timestamps. */
export function fixtureEmployees(): Employee[] {
  return [
    {
      employee_id: "FIX-001",
      full_name: "Fixture Employee Alpha",
      is_active: true,
      created_at: "2026-01-05T09:00:00+00:00",
    },
    {
      employee_id: "FIX-002",
      full_name: "Fixture Employee Beta",
      is_active: true,
      created_at: "2026-01-06T09:00:00+00:00",
    },
    {
      employee_id: "FIX-003",
      full_name: "Fixture Employee Gamma",
      is_active: true,
      created_at: "2026-02-01T09:00:00+00:00",
    },
    {
      employee_id: "FIX-004",
      full_name: "Fixture Employee Delta",
      is_active: false,
      created_at: "2026-02-14T09:00:00+00:00",
    },
    {
      employee_id: "FIX-005",
      full_name: "Fixture Employee Epsilon",
      is_active: false,
      created_at: null,
    },
    {
      employee_id: "FIX-006",
      full_name: "Fixture Employee Zeta",
      is_active: true,
      created_at: "2026-03-01T09:00:00+00:00",
    },
  ];
}

/**
 * Synthetic journal events placed shortly before `now`, so the "events today"
 * metric is meaningful. FIX-001 and FIX-003 are open sessions; FIX-002 checked
 * out; FIX-006 has a check-in and a later check-out. The trailing record is
 * reported as incomplete so the warning state is demonstrable.
 */
export function fixtureAttendance(nowMs: number): AttendanceResponse {
  const at = (minutesAgo: number): string =>
    new Date(nowMs - minutesAgo * 60_000).toISOString();

  const events = [
    {
      event_id: "FIX-EVT-0001",
      employee_id: "FIX-001",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(240),
    },
    {
      event_id: "FIX-EVT-0002",
      employee_id: "FIX-002",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(210),
    },
    {
      event_id: "FIX-EVT-0003",
      employee_id: "FIX-002",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(150),
    },
    {
      event_id: "FIX-EVT-0004",
      employee_id: "FIX-003",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(120),
    },
    {
      event_id: "FIX-EVT-0005",
      employee_id: "FIX-006",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(90),
    },
    {
      event_id: "FIX-EVT-0006",
      employee_id: "FIX-006",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(30),
    },
  ];

  return {
    events,
    count: events.length,
    incomplete_trailing_record: true,
  };
}

export interface FixtureModeEnv {
  readonly NODE_ENV?: string;
  readonly DASHBOARD_DATA_MODE?: string;
}

/**
 * Fixture mode requires **both** a development build and an explicit opt-in.
 * In a production build it is impossible, whatever `DASHBOARD_DATA_MODE` says.
 */
export function isFixtureModeEnabled(env: FixtureModeEnv): boolean {
  if (env.NODE_ENV !== "development") {
    return false;
  }
  return (env.DASHBOARD_DATA_MODE ?? "").trim().toLowerCase() === "fixtures";
}
