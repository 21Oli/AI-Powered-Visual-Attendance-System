import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AttendanceView } from "../src/components/attendance/AttendanceView";
import { CreateEmployeeForm } from "../src/components/employees/CreateEmployeeForm";
import { EmployeeDetail } from "../src/components/employees/EmployeeDetail";
import { EmployeesView } from "../src/components/employees/EmployeesView";
import { ACCESS_DENIED_MESSAGE } from "../src/lib/api/errors";
import { USER_ROLES } from "../src/lib/api/types";
import { withRole } from "./helpers/auth";

/**
 * Which privileged controls exist for which role.
 *
 * The toolbar, table and detail panel are asserted separately because each one
 * decides for itself; a control missing in one place but present in another
 * would be exactly the leak this milestone is about. Every case also asserts
 * that the *page* still renders for the role — nothing here hides readable data.
 */

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const employee = {
  employee_id: "EMP-1",
  full_name: "Ada Example",
  is_active: true,
  created_at: "2026-01-01T00:00:00+00:00",
};

function routeApi(rows: readonly unknown[] = [employee]) {
  fetchMock.mockImplementation((input) => {
    const url = String(input);
    if (/\/employees\/[^/?]+$/.test(url)) {
      return Promise.resolve(json(employee));
    }
    return Promise.resolve(
      json({ employees: rows, count: rows.length, limit: 20, next_after: null }),
    );
  });
}

const createButton = () =>
  screen.queryByRole("button", { name: /create employee/i });

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("employee actions per role", () => {
  it("offers creation and deactivation to Admin", async () => {
    routeApi();
    render(withRole("Admin", <EmployeesView />));

    await screen.findByText("EMP-1");
    expect(createButton()).not.toBeNull();
    expect(screen.getByRole("button", { name: "Deactivate" })).toBeDefined();
  });

  it("offers creation but not deactivation to HR Manager", async () => {
    routeApi();
    render(withRole("HR Manager", <EmployeesView />));

    await screen.findByText("EMP-1");
    expect(createButton()).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Deactivate" })).toBeNull();
  });

  for (const role of ["Operator", "Auditor"] as const) {
    it(`offers no write control to ${role}`, async () => {
      routeApi();
      render(withRole(role, <EmployeesView />));

      // The readable directory still renders.
      await screen.findByText("EMP-1");
      expect(createButton()).toBeNull();
      expect(screen.queryByRole("button", { name: "Deactivate" })).toBeNull();
      expect(screen.getByRole("button", { name: "View" })).toBeDefined();
    });
  }

  it("does not invite creation in the empty state for a read-only role", async () => {
    routeApi([]);
    render(withRole("Auditor", <EmployeesView />));

    expect(
      await screen.findByText(/no employees are registered yet/i),
    ).toBeDefined();
    expect(screen.queryByText(/create the first employee/i)).toBeNull();
  });

  it("refuses a create submitted outside a permitted role", async () => {
    const onCreated = vi.fn();
    const user = userEvent.setup();
    render(
      withRole(
        "Operator",
        <CreateEmployeeForm onCreated={onCreated} onCancel={() => undefined} />,
      ),
    );

    await user.type(screen.getByLabelText(/employee id/i), "EMP-9");
    await user.type(screen.getByLabelText(/full name/i), "Grace Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    expect(screen.getByRole("alert").textContent).toContain(
      ACCESS_DENIED_MESSAGE,
    );
    expect(fetchMock).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("shows an auditor a read-only detail panel", async () => {
    routeApi();
    render(
      withRole(
        "Auditor",
        <EmployeeDetail
          employeeId="EMP-1"
          onClose={() => undefined}
          onRenamed={() => undefined}
          onRequestDeactivate={() => undefined}
        />,
      ),
    );

    expect(await screen.findByText("Ada Example")).toBeDefined();
    expect(screen.queryByRole("button", { name: "Save name" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /deactivate employee/i }),
    ).toBeNull();
    expect(screen.getByText(/can view this record but not change it/i)).toBeDefined();
  });

  it("shows an HR Manager the rename form without deactivation", async () => {
    routeApi();
    render(
      withRole(
        "HR Manager",
        <EmployeeDetail
          employeeId="EMP-1"
          onClose={() => undefined}
          onRenamed={() => undefined}
          onRequestDeactivate={() => undefined}
        />,
      ),
    );

    expect(await screen.findByRole("button", { name: "Save name" })).toBeDefined();
    expect(
      screen.queryByRole("button", { name: /deactivate employee/i }),
    ).toBeNull();
  });
});

describe("check-out action per role", () => {
  it("offers the operator panel to Admin, HR Manager and Operator", () => {
    for (const role of USER_ROLES.filter((entry) => entry !== "Auditor")) {
      const { unmount } = render(
        withRole(role, <AttendanceView dataSourceKind="fixture" />),
      );
      expect(
        screen.getByRole("region", { name: "Operator checkout" }),
      ).toBeDefined();
      unmount();
    }
  });

  it("hides the operator panel from an auditor but keeps the journal", async () => {
    render(withRole("Auditor", <AttendanceView dataSourceKind="fixture" />));

    expect(
      screen.queryByRole("region", { name: "Operator checkout" }),
    ).toBeNull();
    expect(
      screen.getByText(/check-out is available to roles with the operator/i),
    ).toBeDefined();
    expect(
      await screen.findByRole("table", {
        name: "Attendance journal, in append order",
      }),
    ).toBeDefined();
  });
});
