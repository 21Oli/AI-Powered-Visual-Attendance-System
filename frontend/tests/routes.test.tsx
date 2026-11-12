import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AttendancePage from "../src/app/(dashboard)/attendance/page";
import EmployeesPage from "../src/app/(dashboard)/employees/page";
import OverviewPage from "../src/app/(dashboard)/overview/page";
import SystemStatusPage from "../src/app/(dashboard)/system-status/page";
import RootPage from "../src/app/page";
import { DEFAULT_ROUTE } from "../src/lib/navigation";

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation((input) => {
    const url = String(input);
    if (url.includes("/health")) {
      return Promise.resolve(json({ status: "ok" }));
    }
    return url.includes("/attendance")
      ? Promise.resolve(json({ events: [], count: 0, incomplete_trailing_record: false }))
      : Promise.resolve(json({ employees: [], count: 0, limit: 100, next_after: null }));
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("root route", () => {
  it("redirects to the overview area", () => {
    let thrown: unknown;
    try {
      RootPage();
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(Error);
    expect(DEFAULT_ROUTE).toBe("/overview");
    expect(String((thrown as { digest?: string }).digest)).toContain(DEFAULT_ROUTE);
  });
});

describe("overview route", () => {
  it("renders the functional dashboard instead of a placeholder", async () => {
    render(<OverviewPage />);

    expect(screen.getByRole("heading", { level: 1, name: "Overview" })).toBeDefined();
    expect(await screen.findByText("Total employees")).toBeDefined();
    expect(screen.queryByText(/under development/i)).toBeNull();
  });

  it("leaves fixture mode off unless the server opts in", () => {
    render(<OverviewPage />);
    expect(screen.queryByRole("note")).toBeNull();
  });
});

describe("employees route", () => {
  it("renders the employee directory instead of a placeholder", async () => {
    render(<EmployeesPage />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Employees" }),
    ).toBeDefined();
    expect(
      await screen.findByText(/no employees are registered yet/i),
    ).toBeDefined();
    expect(screen.queryByText(/under development/i)).toBeNull();
  });
});

describe("attendance route", () => {
  it("renders the read-only journal instead of a placeholder", async () => {
    render(<AttendancePage />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Attendance" }),
    ).toBeDefined();
    expect(
      await screen.findByText(/journal contains no events/i),
    ).toBeDefined();
    expect(screen.queryByText(/under development/i)).toBeNull();
  });
});

describe("system status route", () => {
  it("renders the backend API health screen instead of a placeholder", async () => {
    render(<SystemStatusPage />);

    expect(
      screen.getByRole("heading", { level: 1, name: "System Status" }),
    ).toBeDefined();
    expect(
      await screen.findByText(/Backend API reachable/),
    ).toBeDefined();
    expect(screen.queryByText(/under development/i)).toBeNull();
    expect(screen.queryByText(/structural placeholder only/i)).toBeNull();
  });
});
