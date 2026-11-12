import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AttendanceView } from "../src/components/attendance/AttendanceView";
import type { AttendanceEvent, Employee } from "../src/lib/api/types";

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

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

function employeesPage(rows: readonly Employee[], next: string | null) {
  return { employees: rows, count: rows.length, limit: 100, next_after: next };
}

function attendancePayload(events: readonly AttendanceEvent[], incomplete = false) {
  return { events, count: events.length, incomplete_trailing_record: incomplete };
}

interface Handlers {
  attendance?: () => Promise<Response>;
  employees?: () => Promise<Response>;
}

function routeApi(handlers: Handlers = {}) {
  fetchMock.mockImplementation((input) => {
    const url = String(input);
    if (url.includes("/employees")) {
      return handlers.employees
        ? handlers.employees()
        : Promise.resolve(json(employeesPage([], null)));
    }
    return handlers.attendance
      ? handlers.attendance()
      : Promise.resolve(json(attendancePayload([])));
  });
}

function bodyRows(): HTMLElement[] {
  const table = screen.getByRole("table", {
    name: "Attendance journal, in append order",
  });
  return within(table)
    .getAllByRole("row")
    .filter((row) => row.querySelector("td") !== null);
}

/** Seven events across three UTC days, plus an undated and an unknown one. */
const dataset: AttendanceEvent[] = [
  event("ev1", "A", "check_in", "2026-11-11T08:00:00Z"),
  event("ev2", "A", "check_out", "2026-11-11T12:00:00Z"),
  event("ev3", "B", "check_in", "2026-11-11T09:00:00Z"),
  event("ev4", "B", "check_in", "2026-11-12T09:00:00Z"),
  event("ev5", "C", "lunch_break", "2026-11-12T10:00:00Z"),
  event("ev6", "D", "check_in", "not-a-date"),
  event("ev7", "E", "check_out", "2026-11-10T17:00:00Z"),
];

function routeDataset(handlers: Handlers = {}) {
  routeApi({
    attendance: () => Promise.resolve(json(attendancePayload(dataset))),
    employees: () =>
      Promise.resolve(
        json(
          employeesPage(
            [employee("A", "Ada Fixture"), employee("B", "Grace Fixture"), employee("C", "Alan Fixture")],
            null,
          ),
        ),
      ),
    ...handlers,
  });
}

/** 24 check-ins for one employee on one UTC day. */
function manyEvents(count: number): AttendanceEvent[] {
  return Array.from({ length: count }, (_, index) =>
    event(
      `p${index}`,
      "A",
      "check_in",
      `2026-11-11T00:${String(index).padStart(2, "0")}:00Z`,
    ),
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("attendance page: data loading", () => {
  it("walks every employee page and loads the journal concurrently", async () => {
    let employeeCalls = 0;
    routeApi({
      employees: () => {
        employeeCalls += 1;
        return Promise.resolve(
          json(
            employeeCalls === 1
              ? employeesPage([employee("A", "Ada Fixture")], "A")
              : employeesPage([employee("B", "Grace Fixture")], null),
          ),
        );
      },
      attendance: () =>
        Promise.resolve(
          json(attendancePayload([event("1", "A", "check_in", "2026-11-11T08:00:00Z")])),
        ),
    });

    render(<AttendanceView dataSourceKind="live" />);

    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });
    // Both pages of the directory reach the employee filter options.
    expect(screen.getByRole("option", { name: "Ada Fixture (A)" })).toBeDefined();
    expect(screen.getByRole("option", { name: "Grace Fixture (B)" })).toBeDefined();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });
    // Call order: employee page 1, journal, then employee page 2 (after=A).
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "/api/backend/api/v1/attendance",
    );
    expect(fetchMock.mock.calls[2]?.[0]).toContain("after=A");
  });

  it("renders events in append order with UTC timestamps and session state", async () => {
    routeApi({
      attendance: () =>
        Promise.resolve(
          json(
            attendancePayload([
              event("1", "A", "check_out", "2026-11-11T18:00:00Z"),
              event("2", "A", "check_in", "2026-11-11T08:00:00Z"),
            ]),
          ),
        ),
      employees: () =>
        Promise.resolve(json(employeesPage([employee("A", "Ada Fixture")], null))),
    });

    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    const rows = bodyRows();
    expect(rows[0]?.textContent).toContain("Check-out");
    expect(rows[0]?.textContent).toContain("2026-11-11 18:00:00 UTC");
    expect(rows[1]?.textContent).toContain("Check-in");
    expect(rows[1]?.textContent).toContain("2026-11-11 08:00:00 UTC");
    // The latest valid event is the check-out, so both rows read Closed.
    expect(rows[0]?.textContent).toContain("Closed");
    expect(rows[1]?.textContent).toContain("Closed");
  });

  it("keeps the journal when the employee directory fails", async () => {
    routeApi({
      employees: () => Promise.reject(new TypeError("Failed to fetch")),
      attendance: () =>
        Promise.resolve(
          json(attendancePayload([event("1", "A", "check_in", "2026-11-11T08:00:00Z")])),
        ),
    });

    render(<AttendanceView dataSourceKind="live" />);

    expect(
      await screen.findByText("Employee details unavailable"),
    ).toBeDefined();
    expect(screen.getByText(/NETWORK_UNAVAILABLE/)).toBeDefined();
    expect(screen.getByText("2026-11-11 08:00:00 UTC")).toBeDefined();
    expect(
      (screen.getByLabelText("Employee") as HTMLSelectElement).disabled,
    ).toBe(true);
  });

  it("keeps the loaded directory when the journal fails", async () => {
    routeApi({
      employees: () =>
        Promise.resolve(json(employeesPage([employee("A", "Ada Fixture")], null))),
      attendance: () =>
        Promise.resolve(
          json(
            {
              error: {
                code: "ATTENDANCE_UNAVAILABLE",
                message: "The attendance journal could not be read.",
              },
            },
            503,
          ),
        ),
    });

    render(<AttendanceView dataSourceKind="live" />);

    expect(await screen.findByText("Attendance is unavailable")).toBeDefined();
    expect(screen.getByText(/ATTENDANCE_UNAVAILABLE/)).toBeDefined();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(/Employee details are unavailable/i)).toBeNull();
  });

  it("reports both failures independently", async () => {
    routeApi({
      employees: () => Promise.reject(new TypeError("Failed to fetch")),
      attendance: () => Promise.reject(new TypeError("Failed to fetch")),
    });

    render(<AttendanceView dataSourceKind="live" />);

    await waitFor(() => {
      expect(screen.getAllByRole("alert")).toHaveLength(2);
    });
    expect(screen.getByText("Attendance is unavailable")).toBeDefined();
    expect(screen.getByText("Employee details are unavailable")).toBeDefined();
  });

  it("shows the torn-journal warning", async () => {
    routeApi({
      attendance: () =>
        Promise.resolve(json(attendancePayload(dataset, true))),
      employees: () => Promise.resolve(json(employeesPage([], null))),
    });

    render(<AttendanceView dataSourceKind="live" />);

    expect(await screen.findByText("Journal integrity warning")).toBeDefined();
    expect(screen.getByText(/incomplete trailing record/i)).toBeDefined();
  });

  it("shows an empty journal without inventing events", async () => {
    routeApi();

    render(<AttendanceView dataSourceKind="live" />);

    expect(
      await screen.findByText(/journal contains no events/i),
    ).toBeDefined();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText(/Page 1 of 1/)).toBeDefined();
  });

  it("labels an employee id missing from the directory", async () => {
    routeApi({
      attendance: () =>
        Promise.resolve(
          json(attendancePayload([event("1", "Z", "check_in", "2026-11-11T08:00:00Z")])),
        ),
      employees: () =>
        Promise.resolve(json(employeesPage([employee("A", "Ada Fixture")], null))),
    });

    render(<AttendanceView dataSourceKind="live" />);

    const table = await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });
    expect(within(table).getByText("Unknown employee")).toBeDefined();
    expect(within(table).getByText("Z")).toBeDefined();
  });

  it("shows an unknown event type and an unreadable timestamp", async () => {
    routeDataset();

    render(<AttendanceView dataSourceKind="live" />);

    expect(
      await screen.findByText("Unknown type (lunch_break)"),
    ).toBeDefined();
    expect(screen.getByText("Unavailable")).toBeDefined();
    expect(screen.getByText(/unreadable timestamp/i)).toBeDefined();
  });

  it("refreshes both resources on demand", async () => {
    let attendanceCalls = 0;
    routeApi({
      attendance: () => {
        attendanceCalls += 1;
        return Promise.resolve(
          json(
            attendancePayload(
              attendanceCalls === 1
                ? [event("1", "A", "check_in", "2026-11-11T08:00:00Z")]
                : [
                    event("1", "A", "check_in", "2026-11-11T08:00:00Z"),
                    event("2", "A", "check_out", "2026-11-11T12:00:00Z"),
                  ],
            ),
          ),
        );
      },
      employees: () =>
        Promise.resolve(json(employeesPage([employee("A", "Ada Fixture")], null))),
    });

    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByText("2026-11-11 08:00:00 UTC");

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    expect(await screen.findByText("2026-11-11 12:00:00 UTC")).toBeDefined();
    expect(bodyRows()).toHaveLength(2);
  });

  it("ignores a stale journal response that resolves after a newer one", async () => {
    let attendanceCalls = 0;
    let resolveFirst: (response: Response) => void = () => undefined;
    const firstAttendance = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });

    routeApi({
      employees: () =>
        Promise.resolve(json(employeesPage([employee("A", "Ada Fixture")], null))),
      attendance: () => {
        attendanceCalls += 1;
        return attendanceCalls === 1
          ? firstAttendance
          : Promise.resolve(
              json(
                attendancePayload([
                  event("fresh", "A", "check_in", "2026-11-11T08:00:00Z"),
                ]),
              ),
            );
      },
    });

    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);

    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByText("2026-11-11 08:00:00 UTC");

    resolveFirst(
      json(
        attendancePayload([
          event("stale", "A", "check_in", "2026-11-10T08:00:00Z"),
        ]),
      ),
    );
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(screen.queryByText("2026-11-10 08:00:00 UTC")).toBeNull();
    expect(screen.getByText("2026-11-11 08:00:00 UTC")).toBeDefined();
  });
});

describe("attendance page: filters", () => {
  it("offers the UTC days present in the journal plus All dates", async () => {
    routeDataset();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    const options = within(
      screen.getByLabelText("Date (UTC)") as HTMLSelectElement,
    ).getAllByRole("option");

    expect(options).toHaveLength(4);
    expect(options[0]?.textContent).toBe("All dates");
    expect(options[1]?.textContent).toBe("2026-11-12 (UTC)");
  });

  it("filters by one UTC day and keeps undated records under All dates", async () => {
    routeDataset();
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    expect(screen.getByText("Showing 7 of 7 journal events")).toBeDefined();
    expect(screen.getByText("Unavailable")).toBeDefined();

    await user.selectOptions(screen.getByLabelText("Date (UTC)"), "2026-11-11");

    expect(screen.getByText("Showing 3 of 7 journal events")).toBeDefined();
    expect(screen.queryByText("Unavailable")).toBeNull();
  });

  it("filters by event type", async () => {
    routeDataset();
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    await user.selectOptions(screen.getByLabelText("Event type"), "check_in");
    expect(screen.getByText("Showing 4 of 7 journal events")).toBeDefined();

    await user.selectOptions(screen.getByLabelText("Event type"), "check_out");
    expect(screen.getByText("Showing 2 of 7 journal events")).toBeDefined();
  });

  it("filters by employee", async () => {
    routeDataset();
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    await user.selectOptions(screen.getByLabelText("Employee"), "A");
    expect(screen.getByText("Showing 2 of 7 journal events")).toBeDefined();

    await user.selectOptions(screen.getByLabelText("Employee"), "B");
    expect(screen.getByText("Showing 2 of 7 journal events")).toBeDefined();
  });

  it("filters by session status without treating unknown as closed", async () => {
    routeDataset();
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    await user.selectOptions(screen.getByLabelText("Session status"), "open");
    expect(screen.getByText("Showing 2 of 7 journal events")).toBeDefined();

    await user.selectOptions(screen.getByLabelText("Session status"), "closed");
    // A (check-out last) and E (check-out only); C and D have no state.
    expect(screen.getByText("Showing 3 of 7 journal events")).toBeDefined();
  });

  it("composes every filter and resets to all events", async () => {
    routeDataset();
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    await user.selectOptions(screen.getByLabelText("Date (UTC)"), "2026-11-11");
    await user.selectOptions(screen.getByLabelText("Event type"), "check_in");
    await user.selectOptions(screen.getByLabelText("Employee"), "B");
    await user.selectOptions(screen.getByLabelText("Session status"), "open");

    expect(screen.getByText("Showing 1 of 7 journal events")).toBeDefined();

    await user.click(screen.getByRole("button", { name: "Reset filters" }));

    expect(screen.getByText("Showing 7 of 7 journal events")).toBeDefined();
    expect(screen.getByText("Unavailable")).toBeDefined();
  });

  it("keeps journal order after filtering", async () => {
    routeDataset();
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    await user.selectOptions(screen.getByLabelText("Employee"), "A");

    const rows = bodyRows();
    expect(rows[0]?.textContent).toContain("2026-11-11 08:00:00 UTC");
    expect(rows[1]?.textContent).toContain("2026-11-11 12:00:00 UTC");
  });
});

describe("attendance page: pagination", () => {
  function routeMany(count: number, next = count) {
    routeApi({
      attendance: () =>
        Promise.resolve(json(attendancePayload(manyEvents(next)))),
      employees: () =>
        Promise.resolve(json(employeesPage([employee("A", "Ada Fixture")], null))),
    });
    return count;
  }

  it("defaults to 20 rows and reports page and range", async () => {
    routeMany(24);
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    expect(screen.getByText(/Page 1 of 2/)).toBeDefined();
    expect(screen.getByText(/Events 1–20 of 24/)).toBeDefined();
    expect(bodyRows()).toHaveLength(20);
  });

  it("moves between pages without requesting more journal data", async () => {
    routeMany(24);
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });
    const callsBefore = fetchMock.mock.calls.length;

    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.getByText(/Page 2 of 2/)).toBeDefined();
    expect(screen.getByText(/Events 21–24 of 24/)).toBeDefined();
    expect(bodyRows()).toHaveLength(4);
    expect(fetchMock.mock.calls.length).toBe(callsBefore);
    expect(
      screen.getByRole("button", { name: "Next" }).getAttribute("disabled"),
    ).not.toBeNull();
  });

  it("supports every page size and resets to page one", async () => {
    routeMany(24);
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText(/Page 2 of 2/)).toBeDefined();

    await user.selectOptions(screen.getByLabelText("Rows per page"), "10");

    expect(screen.getByText(/Page 1 of 3/)).toBeDefined();
    expect(bodyRows()).toHaveLength(10);

    await user.selectOptions(screen.getByLabelText("Rows per page"), "100");
    expect(screen.getByText(/Page 1 of 1/)).toBeDefined();
    expect(bodyRows()).toHaveLength(24);
  });

  it("resets the page when a filter changes and handles an empty result", async () => {
    routeMany(24);
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    await user.selectOptions(screen.getByLabelText("Rows per page"), "10");
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText(/Page 2 of 3/)).toBeDefined();

    await user.selectOptions(screen.getByLabelText("Event type"), "check_out");

    expect(screen.getByText("No events match these filters.")).toBeDefined();
    expect(screen.getByText(/Page 1 of 1/)).toBeDefined();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("clamps the page when a refresh returns fewer events", async () => {
    let attendanceCalls = 0;
    routeApi({
      attendance: () => {
        attendanceCalls += 1;
        return Promise.resolve(
          json(attendancePayload(manyEvents(attendanceCalls === 1 ? 24 : 3))),
        );
      },
      employees: () =>
        Promise.resolve(json(employeesPage([employee("A", "Ada Fixture")], null))),
    });

    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    await user.selectOptions(screen.getByLabelText("Rows per page"), "10");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText(/Page 3 of 3/)).toBeDefined();

    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => {
      expect(screen.getByText(/Page 1 of 1/)).toBeDefined();
    });
    expect(screen.getByText(/Events 1–3 of 3/)).toBeDefined();
  });
});

describe("attendance fixtures", () => {
  it("labels synthetic data and never touches the network", async () => {
    render(<AttendanceView dataSourceKind="fixture" />);

    const note = await screen.findByRole("note");
    expect(note.textContent).toContain("FIXTURE DATA");
    expect(note.textContent).toContain("DEVELOPMENT ONLY");
    expect(note.textContent).toMatch(/synthetic/i);
    expect(fetchMock).not.toHaveBeenCalled();

    expect(screen.getAllByText("Fixture Employee Alpha").length).toBeGreaterThan(
      0,
    );
    expect(screen.getByText(/Page 1 of 2/)).toBeDefined();
    expect(bodyRows()).toHaveLength(20);
  });

  it("supports filters and pagination over the synthetic journal", async () => {
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="fixture" />);
    await screen.findByRole("table");

    const options = within(
      screen.getByLabelText("Date (UTC)") as HTMLSelectElement,
    ).getAllByRole("option");
    expect(options).toHaveLength(4);

    await user.selectOptions(screen.getByLabelText("Event type"), "check_out");
    expect(screen.getByText(/Showing \d+ of 25 journal events/)).toBeDefined();

    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    expect(screen.getByText("Showing 25 of 25 journal events")).toBeDefined();
  });

  it("never replaces a failed live request with fixture data", async () => {
    routeApi({
      attendance: () => Promise.reject(new TypeError("Failed to fetch")),
      employees: () => Promise.reject(new TypeError("Failed to fetch")),
    });

    render(<AttendanceView dataSourceKind="live" />);

    await screen.findByText("Attendance is unavailable");
    expect(screen.queryByRole("note")).toBeNull();
    expect(screen.queryByText(/FIXTURE DATA/)).toBeNull();
  });
});
