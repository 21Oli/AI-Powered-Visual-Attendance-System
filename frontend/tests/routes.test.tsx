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
  fetchMock.mockImplementation((input) =>
    String(input).includes("/attendance")
      ? Promise.resolve(json({ events: [], count: 0, incomplete_trailing_record: false }))
      : Promise.resolve(json({ employees: [], count: 0, limit: 100, next_after: null })),
  );
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

const placeholders = [
  { title: "Attendance", Page: AttendancePage, purpose: /attendance journal/i },
  {
    title: "System Status",
    Page: SystemStatusPage,
    purpose: /service availability/i,
  },
] as const;

describe("remaining dashboard placeholder routes", () => {
  it.each(placeholders)(
    "renders the $title placeholder",
    ({ title, Page, purpose }) => {
      render(<Page />);

      expect(screen.getByRole("heading", { level: 1, name: title })).toBeDefined();
      expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
      expect(screen.getByText(purpose)).toBeDefined();
      expect(screen.getByText(/under development/i)).toBeDefined();
      expect(screen.getByText(/structural placeholder only/i)).toBeDefined();
    },
  );

  it.each(placeholders)("shows no fabricated data on $title", ({ Page }) => {
    render(<Page />);

    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(/fixture/i)).toBeNull();
    expect(screen.queryByText(/^\d+%$/)).toBeNull();
  });
});
