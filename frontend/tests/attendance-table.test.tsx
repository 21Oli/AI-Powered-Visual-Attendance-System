import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AttendanceEvent, Employee } from "../src/lib/api/types";
import {
  deriveSessionStates,
  foldLatestEvents,
} from "../src/lib/attendance/journal";
import type { SessionState } from "../src/lib/attendance/journal";
import {
  AttendanceTable,
  type DirectoryState,
} from "../src/components/attendance/AttendanceTable";

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

const employee = (id: string, name: string, active = true): Employee => ({
  employee_id: id,
  full_name: name,
  is_active: active,
  created_at: null,
});

const directory = new Map<string, Employee>([
  ["A", employee("A", "Ada Fixture")],
  ["B", employee("B", "Grace Fixture", false)],
]);

function sessions(events: readonly AttendanceEvent[]): ReadonlyMap<string, SessionState> {
  return deriveSessionStates(foldLatestEvents(events));
}

function renderTable(
  events: readonly AttendanceEvent[],
  options: {
    employees?: ReadonlyMap<string, Employee> | null;
    directoryState?: DirectoryState;
  } = {},
) {
  const employees =
    options.employees === undefined ? directory : options.employees;
  return render(
    <AttendanceTable
      events={events}
      employees={employees}
      directoryState={options.directoryState ?? "ready"}
      sessionStates={sessions(events)}
    />,
  );
}

function bodyRows(): HTMLElement[] {
  const table = screen.getByRole("table", {
    name: "Attendance journal, in append order",
  });
  return within(table)
    .getAllByRole("row")
    .filter((row) => row.querySelector("td") !== null);
}

describe("attendance table", () => {
  it("keeps journal append order rather than sorting by timestamp", () => {
    renderTable([
      event("1", "A", "check_out", "2026-11-11T18:00:00Z"),
      event("2", "A", "check_in", "2026-11-11T08:00:00Z"),
      event("3", "B", "check_in", "2026-11-11T07:00:00Z"),
    ]);

    const rows = bodyRows();
    expect(rows).toHaveLength(3);
    expect(rows[0]?.textContent).toContain("2026-11-11 18:00:00 UTC");
    expect(rows[1]?.textContent).toContain("2026-11-11 08:00:00 UTC");
    expect(rows[2]?.textContent).toContain("2026-11-11 07:00:00 UTC");
  });

  it("resolves names, keeps the id, and marks inactive employees", () => {
    renderTable([
      event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
      event("2", "B", "check_in", "2026-11-11T09:00:00Z"),
    ]);

    const rows = bodyRows();
    expect(rows[0]?.textContent).toContain("Ada Fixture");
    expect(rows[0]?.textContent).toContain("A");
    expect(rows[1]?.textContent).toContain("Grace Fixture");
    expect(rows[1]?.textContent).toContain("inactive");
  });

  it("labels an id missing from the directory without inventing a name", () => {
    renderTable([event("1", "Z", "check_in", "2026-11-11T08:00:00Z")]);

    expect(screen.getByText("Unknown employee")).toBeDefined();
    expect(screen.getByText("Z")).toBeDefined();
    expect(screen.queryByText(/fixture employee z/i)).toBeNull();
  });

  it("states that employee details are unavailable rather than guessing", () => {
    renderTable([event("1", "A", "check_in", "2026-11-11T08:00:00Z")], {
      employees: null,
      directoryState: "unavailable",
    });

    expect(screen.getByText("Employee details unavailable")).toBeDefined();
    expect(screen.queryByText("Ada Fixture")).toBeNull();
  });

  it("shows a loading label while the directory is still loading", () => {
    renderTable([event("1", "A", "check_in", "2026-11-11T08:00:00Z")], {
      employees: null,
      directoryState: "loading",
    });

    expect(screen.getByText("Loading employee…")).toBeDefined();
  });

  it("shows an unknown event type and an unreadable timestamp explicitly", () => {
    renderTable([
      event("1", "A", "lunch_break", "2026-11-11T10:00:00Z"),
      event("2", "A", "check_in", "not-a-date"),
    ]);

    expect(screen.getByText("Unknown type (lunch_break)")).toBeDefined();
    expect(screen.getByText("Unavailable")).toBeDefined();
    expect(screen.queryByText(/invalid date/i)).toBeNull();
  });

  it("derives the session column from the shared fold", () => {
    renderTable([
      event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
      event("2", "B", "check_in", "2026-11-11T08:30:00Z"),
      event("3", "B", "check_out", "2026-11-11T09:00:00Z"),
      event("4", "C", "lunch_break", "2026-11-11T10:00:00Z"),
    ]);

    const rows = bodyRows();
    expect(rows[0]?.textContent).toContain("Open");
    // B's latest valid event is a check-out, so both of its rows read Closed.
    expect(rows[1]?.textContent).toContain("Closed");
    expect(rows[2]?.textContent).toContain("Closed");
    // C has no valid boundary at all.
    expect(rows[3]?.textContent).toContain("Unknown");
  });

  it("resolves an exact timestamp tie by the later journal position", () => {
    const sameInstant = "2026-11-11T08:00:00Z";
    renderTable([
      event("1", "A", "check_in", sameInstant),
      event("2", "A", "check_out", sameInstant),
    ]);

    for (const row of bodyRows()) {
      expect(row.textContent).toContain("Closed");
    }
  });

  it("explains that the session column is not a per-row session", () => {
    renderTable([event("1", "A", "check_in", "2026-11-11T08:00:00Z")]);

    expect(
      screen.getByText(/not a separate session per row/i),
    ).toBeDefined();
  });
});
