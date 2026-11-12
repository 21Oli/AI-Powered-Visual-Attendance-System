import { EVENT_TYPE_CHECK_IN, EVENT_TYPE_CHECK_OUT } from "../api/types";
import type { AttendanceEvent, AttendanceResponse, Employee } from "../api/types";

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

/** UTC midnight of the day `daysAgo` days before the clock instant. */
function utcDayStart(nowMs: number, daysAgo: number): number {
  const now = new Date(nowMs);
  return (
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) -
    daysAgo * 86_400_000
  );
}

/** A UTC instant on a given day, at `hours`:`minutes`. */
function at(dayStartMs: number, hours: number, minutes: number): string {
  return new Date(
    dayStartMs + hours * 3_600_000 + minutes * 60_000,
  ).toISOString();
}

/**
 * A deterministic synthetic journal spanning three UTC calendar days, plus one
 * unreadable timestamp.
 *
 * States the Attendance page can be exercised with: FIX-001 and FIX-003 are
 * open (latest event is a check-in), FIX-002 and FIX-006 are closed, and the two
 * **inactive** employees FIX-004/FIX-005 hold historical open sessions (their
 * only check-ins sit two days back). FIX-005 has no recent activity at all. The
 * final record is undated, which is legal for the contract (any non-empty
 * string) and must never be turned into a fabricated date.
 *
 * The last **dated** record sits at the fixture clock instant, so the journal
 * always contains a record for "now" and two different clocks never produce
 * identical data. The journal is 26 events, so the default 20-row page has a
 * second page. `incomplete_trailing_record` stays true so the warning state is
 * demonstrable.
 */
export function fixtureAttendance(nowMs: number): AttendanceResponse {
  const day0 = utcDayStart(nowMs, 0);
  const day1 = utcDayStart(nowMs, 1);
  const day2 = utcDayStart(nowMs, 2);

  const events = [
    // Two UTC days ago: the inactive employees' only (historical) sessions.
    {
      event_id: "FIX-EVT-0001",
      employee_id: "FIX-001",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day2, 8, 0),
    },
    {
      event_id: "FIX-EVT-0002",
      employee_id: "FIX-002",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day2, 8, 10),
    },
    {
      event_id: "FIX-EVT-0003",
      employee_id: "FIX-003",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day2, 8, 20),
    },
    {
      event_id: "FIX-EVT-0004",
      employee_id: "FIX-004",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day2, 8, 25),
    },
    {
      event_id: "FIX-EVT-0005",
      employee_id: "FIX-005",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day2, 8, 50),
    },
    {
      event_id: "FIX-EVT-0006",
      employee_id: "FIX-006",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day2, 10, 0),
    },
    {
      event_id: "FIX-EVT-0007",
      employee_id: "FIX-001",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(day2, 17, 0),
    },
    {
      event_id: "FIX-EVT-0008",
      employee_id: "FIX-002",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(day2, 16, 30),
    },
    {
      event_id: "FIX-EVT-0009",
      employee_id: "FIX-003",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(day2, 16, 0),
    },

    // Yesterday.
    {
      event_id: "FIX-EVT-0010",
      employee_id: "FIX-001",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day1, 8, 5),
    },
    {
      event_id: "FIX-EVT-0011",
      employee_id: "FIX-002",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day1, 8, 15),
    },
    {
      event_id: "FIX-EVT-0012",
      employee_id: "FIX-003",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day1, 8, 30),
    },
    {
      event_id: "FIX-EVT-0013",
      employee_id: "FIX-006",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day1, 9, 30),
    },
    {
      event_id: "FIX-EVT-0014",
      employee_id: "FIX-001",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(day1, 16, 10),
    },
    {
      event_id: "FIX-EVT-0015",
      employee_id: "FIX-002",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(day1, 16, 45),
    },
    {
      event_id: "FIX-EVT-0016",
      employee_id: "FIX-006",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(day1, 17, 30),
    },

    // Today: FIX-001 stays open, the others close.
    {
      event_id: "FIX-EVT-0017",
      employee_id: "FIX-001",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day0, 8, 5),
    },
    {
      event_id: "FIX-EVT-0018",
      employee_id: "FIX-002",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day0, 8, 10),
    },
    {
      event_id: "FIX-EVT-0019",
      employee_id: "FIX-003",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day0, 8, 20),
    },
    {
      event_id: "FIX-EVT-0020",
      employee_id: "FIX-006",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: at(day0, 9, 0),
    },
    {
      event_id: "FIX-EVT-0021",
      employee_id: "FIX-002",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(day0, 12, 5),
    },
    {
      event_id: "FIX-EVT-0022",
      employee_id: "FIX-006",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(day0, 12, 30),
    },
    {
      event_id: "FIX-EVT-0023",
      employee_id: "FIX-003",
      event_type: EVENT_TYPE_CHECK_OUT,
      occurred_at: at(day0, 18, 10),
    },

    // The most recent dated record, at the fixture clock instant.
    {
      event_id: "FIX-EVT-0024",
      employee_id: "FIX-001",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: new Date(nowMs).toISOString(),
    },

    // Undated record: the contract allows any non-empty string, and a fabricated
    // date here would be worse than the honest "Unavailable".
    {
      event_id: "FIX-EVT-0025",
      employee_id: "FIX-002",
      event_type: EVENT_TYPE_CHECK_IN,
      occurred_at: "not-a-date",
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

/** Type-level helper so `AttendanceEvent` stays the single event schema. */
export type FixtureEvent = AttendanceEvent;
