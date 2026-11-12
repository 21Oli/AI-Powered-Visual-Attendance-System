import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EmployeesView } from "../src/components/employees/EmployeesView";

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const activeEmployee = {
  employee_id: "EMP-1",
  full_name: "Ada Example",
  is_active: true,
  created_at: "2026-01-01T00:00:00+00:00",
};

const deactivatedEmployee = { ...activeEmployee, is_active: false };

let listCalls = 0;

function routeApi(deactivate: () => Promise<Response>) {
  fetchMock.mockImplementation((input, init) => {
    if (init?.method === "POST") {
      return deactivate();
    }
    listCalls += 1;
    const employee = listCalls === 1 ? activeEmployee : deactivatedEmployee;
    return Promise.resolve(
      json({ employees: [employee], count: 1, limit: 20, next_after: null }),
    );
  });
}

function dialog(): HTMLElement {
  return screen.getByRole("dialog");
}

beforeEach(() => {
  fetchMock.mockReset();
  listCalls = 0;
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("employee deactivation", () => {
  it("requires confirmation before calling the API", async () => {
    routeApi(() => Promise.resolve(json(deactivatedEmployee)));
    const user = userEvent.setup();
    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    await user.click(screen.getByRole("button", { name: "Deactivate" }));

    const panel = dialog();
    expect(within(panel).getByText(/Ada Example/)).toBeDefined();
    expect(within(panel).getByText(/EMP-1/)).toBeDefined();
    expect(within(panel).getByText(/no reactivation endpoint/i)).toBeDefined();
    expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(
      false,
    );
  });

  it("cancelling closes the dialog without deactivating", async () => {
    routeApi(() => Promise.resolve(json(deactivatedEmployee)));
    const user = userEvent.setup();
    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    await user.click(screen.getByRole("button", { name: "Deactivate" }));
    await user.click(within(dialog()).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(
      false,
    );
  });

  it("closes on Escape and restores focus to the invoking control", async () => {
    routeApi(() => Promise.resolve(json(deactivatedEmployee)));
    const user = userEvent.setup();
    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    const trigger = screen.getByRole("button", { name: "Deactivate" });
    await user.click(trigger);

    await waitFor(() => {
      expect(document.activeElement).toBe(
        within(dialog()).getByRole("button", { name: "Cancel" }),
      );
    });

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => {
      expect(document.activeElement).toBe(trigger);
    });
    expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(
      false,
    );
  });

  it("deactivates once, reports success and refreshes the directory", async () => {
    routeApi(() => Promise.resolve(json(deactivatedEmployee)));
    const user = userEvent.setup();
    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    await user.click(screen.getByRole("button", { name: "Deactivate" }));
    await user.click(within(dialog()).getByRole("button", { name: "Deactivate" }));

    expect(await screen.findByText(/was deactivated/i)).toBeDefined();
    expect(screen.queryByRole("dialog")).toBeNull();

    const posts = fetchMock.mock.calls.filter((call) => call[1]?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0]?.[0]).toBe("/api/backend/api/v1/employees/EMP-1/deactivate");
    expect(posts[0]?.[1]?.body).toBeUndefined();

    await waitFor(() => {
      expect(screen.getByText("Inactive")).toBeDefined();
    });
    expect(listCalls).toBeGreaterThan(1);
  });

  it("keeps the dialog open and reports a failure", async () => {
    routeApi(() =>
      Promise.resolve(
        json(
          {
            error: {
              code: "EMPLOYEE_UNAVAILABLE",
              message: "The employee register could not be updated.",
            },
          },
          503,
        ),
      ),
    );
    const user = userEvent.setup();
    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    await user.click(screen.getByRole("button", { name: "Deactivate" }));
    await user.click(within(dialog()).getByRole("button", { name: "Deactivate" }));

    const panel = await waitFor(() => dialog());
    expect(await within(panel).findByText(/temporarily unavailable/i)).toBeDefined();
    expect(screen.queryByText(/was deactivated/i)).toBeNull();
    expect(screen.getByText("Active")).toBeDefined();
  });

  it("offers no deactivation for an already inactive employee", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        json({
          employees: [deactivatedEmployee],
          count: 1,
          limit: 20,
          next_after: null,
        }),
      ),
    );
    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    expect(
      screen.getByRole("button", { name: "Deactivate" }).getAttribute("disabled"),
    ).not.toBeNull();
  });
});
