import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import AttendancePage from "../src/app/(dashboard)/attendance/page";
import EmployeesPage from "../src/app/(dashboard)/employees/page";
import OverviewPage from "../src/app/(dashboard)/overview/page";
import SystemStatusPage from "../src/app/(dashboard)/system-status/page";
import RootPage from "../src/app/page";
import { DEFAULT_ROUTE } from "../src/lib/navigation";

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
    expect(String((thrown as { digest?: string }).digest)).toContain(
      DEFAULT_ROUTE,
    );
  });
});

const pages = [
  { title: "Overview", Page: OverviewPage, purpose: /workforce activity/i },
  { title: "Employees", Page: EmployeesPage, purpose: /employee register/i },
  { title: "Attendance", Page: AttendancePage, purpose: /attendance journal/i },
  {
    title: "System Status",
    Page: SystemStatusPage,
    purpose: /service availability/i,
  },
] as const;

describe("dashboard placeholder routes", () => {
  it.each(pages)("renders the $title placeholder", ({ title, Page, purpose }) => {
    render(<Page />);

    expect(
      screen.getByRole("heading", { level: 1, name: title }),
    ).toBeDefined();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByText(purpose)).toBeDefined();
    expect(screen.getByText(/under development/i)).toBeDefined();
    expect(screen.getByText(/structural placeholder only/i)).toBeDefined();
  });

  it.each(pages)(
    "shows no fabricated data on the $title placeholder",
    ({ Page }) => {
      render(<Page />);

      // No tables, no metric values, no fixture banner: placeholders stay empty
      // until the real data wiring lands in later milestones.
      expect(screen.queryByRole("table")).toBeNull();
      expect(screen.queryByText(/fixture/i)).toBeNull();
      expect(screen.queryByText(/^\d+%$/)).toBeNull();
    },
  );
});
