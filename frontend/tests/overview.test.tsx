import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OverviewDashboard } from "../src/components/dashboard/OverviewDashboard";
import { applyTheme } from "../src/lib/theme";

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const employees = [
  { employee_id: "E1", full_name: "Ada", is_active: true, created_at: null },
  { employee_id: "E2", full_name: "Grace", is_active: true, created_at: null },
  { employee_id: "E3", full_name: "Alan", is_active: false, created_at: null },
];

const employeesResponse = {
  employees,
  count: employees.length,
  limit: 100,
  next_after: null,
};

function attendanceResponse(incomplete = false) {
  const now = new Date();
  const iso = new Date(now.getTime() - 60_000).toISOString();
  const events = [
    { event_id: "EV1", employee_id: "E1", event_type: "check_in", occurred_at: iso },
    { event_id: "EV2", employee_id: "E2", event_type: "check_in", occurred_at: iso },
    { event_id: "EV3", employee_id: "E2", event_type: "check_out", occurred_at: iso },
  ];
  return { events, count: events.length, incomplete_trailing_record: incomplete };
}

/** The card for a metric label, so identical numbers stay unambiguous. */
function metric(label: string): HTMLElement {
  const element = screen.getByText(label).closest("li");
  if (!(element instanceof HTMLElement)) {
    throw new Error(`No metric card found for "${label}"`);
  }
  return element;
}

function routeFetch(handlers: {
  employees?: () => Promise<Response>;
  attendance?: () => Promise<Response>;
}) {
  fetchMock.mockImplementation((input) => {
    const url = String(input);
    if (url.includes("/attendance")) {
      return handlers.attendance
        ? handlers.attendance()
        : Promise.resolve(json(attendanceResponse()));
    }
    return handlers.employees
      ? handlers.employees()
      : Promise.resolve(json(employeesResponse));
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Overview", () => {
  it("shows a loading state before data resolves", () => {
    fetchMock.mockReturnValue(new Promise<Response>(() => undefined));

    render(<OverviewDashboard dataSourceKind="live" />);

    expect(screen.getByRole("heading", { level: 1, name: "Overview" })).toBeDefined();
    expect(screen.getAllByText(/loading/i).length).toBeGreaterThan(0);
  });

  it("renders the metrics derived from live responses", async () => {
    routeFetch({});

    render(<OverviewDashboard dataSourceKind="live" />);

    await screen.findByText("Total employees");

    // Three employees, two active, three events today, one open session
    // (E2 is the only employee whose latest event is a check-out).
    expect(within(metric("Total employees")).getByText("3")).toBeDefined();
    expect(within(metric("Active employees")).getByText("2")).toBeDefined();
    expect(within(metric("Events today (UTC)")).getByText("3")).toBeDefined();
    expect(within(metric("Open sessions")).getByText("1")).toBeDefined();
  });

  it("distinguishes an empty register from an unavailable one", async () => {
    routeFetch({
      employees: () =>
        Promise.resolve(json({ employees: [], count: 0, limit: 100, next_after: null })),
    });

    render(<OverviewDashboard dataSourceKind="live" />);

    expect(
      await screen.findByText(/no employees are registered yet/i),
    ).toBeDefined();
    expect(screen.queryByText("Unavailable")).toBeNull();
  });

  it("labels fixture data persistently and never calls the network", async () => {
    render(<OverviewDashboard dataSourceKind="fixture" />);

    expect(await screen.findByRole("note")).toBeDefined();
    expect(screen.getByRole("note").textContent).toContain("FIXTURE DATA");
    expect(screen.getByRole("note").textContent).toContain("DEVELOPMENT ONLY");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the journal integrity warning when the trailer is incomplete", async () => {
    routeFetch({ attendance: () => Promise.resolve(json(attendanceResponse(true))) });

    render(<OverviewDashboard dataSourceKind="live" />);

    expect(await screen.findByText(/journal integrity warning/i)).toBeDefined();
  });

  it("does not claim a healthy journal when attendance is unavailable", async () => {
    routeFetch({
      attendance: () =>
        Promise.resolve(
          json(
            { error: { code: "ATTENDANCE_UNAVAILABLE", message: "The journal could not be read." } },
            503,
          ),
        ),
    });

    render(<OverviewDashboard dataSourceKind="live" />);

    expect(await screen.findByText(/attendance data is unavailable/i)).toBeDefined();
    expect(screen.queryByText(/journal integrity warning/i)).toBeNull();
  });

  it("keeps employee data when attendance fails, and vice versa", async () => {
    routeFetch({
      attendance: () => Promise.reject(new TypeError("Failed to fetch")),
    });

    render(<OverviewDashboard dataSourceKind="live" />);

    expect(await screen.findByText(/attendance data is unavailable/i)).toBeDefined();
    // Employee figures survive: 3 total, 2 active.
    expect(screen.getByText("3")).toBeDefined();
    expect(screen.getByText("2")).toBeDefined();
    // Attendance figures are unavailable, not zero.
    expect(screen.getAllByText("Unavailable").length).toBe(2);
    expect(screen.queryByText("0", { exact: true })).toBeNull();
  });

  it("keeps attendance data when employees fail", async () => {
    routeFetch({
      employees: () => Promise.reject(new TypeError("Failed to fetch")),
    });

    render(<OverviewDashboard dataSourceKind="live" />);

    expect(await screen.findByText(/employee data is unavailable/i)).toBeDefined();
    expect(screen.getAllByText("Unavailable").length).toBe(2);
    expect(screen.getByText("Events today (UTC)")).toBeDefined();
  });

  it("retries a failed section", async () => {
    let attempts = 0;
    routeFetch({
      employees: () => {
        attempts += 1;
        return attempts === 1
          ? Promise.reject(new TypeError("Failed to fetch"))
          : Promise.resolve(json(employeesResponse));
      },
    });

    const user = userEvent.setup();
    render(<OverviewDashboard dataSourceKind="live" />);

    expect(await screen.findByText(/employee data is unavailable/i)).toBeDefined();

    await user.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() => {
      expect(screen.queryByText(/employee data is unavailable/i)).toBeNull();
    });
    expect(screen.getByText("Total employees")).toBeDefined();
  });

  it("offers a keyboard-operable refresh control", async () => {
    routeFetch({});
    const user = userEvent.setup();

    render(<OverviewDashboard dataSourceKind="live" />);
    await screen.findByText("Total employees");

    const refresh = screen.getByRole("button", { name: /refresh/i });
    refresh.focus();
    expect(document.activeElement).toBe(refresh);
    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(fetchMock.mock.calls.length).toBeGreaterThan(2);
    });
  });

  it("never presents a fabricated rate, trend or score", async () => {
    routeFetch({});

    const { container } = render(<OverviewDashboard dataSourceKind="live" />);
    await screen.findByText("Total employees");

    const text = container.textContent ?? "";
    expect(text).not.toContain("%");
    expect(text).not.toMatch(/attendance rate/i);
    expect(text).not.toMatch(/trend/i);
    expect(text).not.toMatch(/accuracy/i);
  });

  it("renders in the light theme and leaves the applied theme alone", async () => {
    // The theme is owned by the shell, not by this page, so it is applied here
    // first and then checked that rendering the dashboard does not clobber it.
    applyTheme("light");
    routeFetch({});

    render(<OverviewDashboard dataSourceKind="live" />);

    expect(await screen.findByText("Total employees")).toBeDefined();
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });
});
