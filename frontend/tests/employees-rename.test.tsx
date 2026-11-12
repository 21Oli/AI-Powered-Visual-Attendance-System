import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EmployeeDetail } from "../src/components/employees/EmployeeDetail";

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

function setup() {
  const onRenamed = vi.fn();
  const onClose = vi.fn();
  const onRequestDeactivate = vi.fn();
  const user = userEvent.setup();
  render(
    <EmployeeDetail
      employeeId="EMP-1"
      onClose={onClose}
      onRenamed={onRenamed}
      onRequestDeactivate={onRequestDeactivate}
    />,
  );
  return { user, onRenamed, onClose, onRequestDeactivate };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("employee detail", () => {
  it("loads the record from the detail endpoint", async () => {
    fetchMock.mockResolvedValueOnce(json(employee));
    setup();

    expect(screen.getByText(/loading employee/i)).toBeDefined();
    expect(await screen.findByText("Ada Example")).toBeDefined();
    expect(screen.getByText("EMP-1")).toBeDefined();
    expect(screen.getByText("Active")).toBeDefined();
    expect(screen.getByText("2026-01-01")).toBeDefined();
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/backend/api/v1/employees/EMP-1",
    );
  });

  it("reports a missing employee", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        { error: { code: "EMPLOYEE_NOT_FOUND", message: "No employee exists." } },
        404,
      ),
    );
    setup();

    expect(await screen.findByText("That employee no longer exists.")).toBeDefined();
  });

  it("rejects a whitespace-only rename before calling the API", async () => {
    fetchMock.mockResolvedValueOnce(json(employee));
    const { user } = setup();
    await screen.findByText("Ada Example");

    const field = screen.getByLabelText(/full name/i);
    await user.clear(field);
    await user.type(field, "   ");
    await user.click(screen.getByRole("button", { name: "Save name" }));

    expect(
      screen.getByText("Enter the employee's full name."),
    ).toBeDefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("renames with full_name only and shows the returned record", async () => {
    fetchMock.mockResolvedValueOnce(json(employee));
    fetchMock.mockResolvedValueOnce(json({ ...employee, full_name: "Ada Lovelace" }));
    const { user, onRenamed } = setup();
    await screen.findByText("Ada Example");

    const field = screen.getByLabelText(/full name/i);
    await user.clear(field);
    await user.type(field, "Ada Lovelace");
    await user.click(screen.getByRole("button", { name: "Save name" }));

    expect(await screen.findByText("Name updated.")).toBeDefined();
    expect(onRenamed).toHaveBeenCalledWith({
      ...employee,
      full_name: "Ada Lovelace",
    });

    const init = fetchMock.mock.calls[1]?.[1];
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(String(init?.body))).toEqual({ full_name: "Ada Lovelace" });
    expect(field).toHaveProperty("value", "Ada Lovelace");
  });

  it("reports a rename conflict on the name field", async () => {
    fetchMock.mockResolvedValueOnce(json(employee));
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: { code: "REQUEST_INVALID", message: "Request validation failed." },
          fields: [{ field: "body.full_name", message: "Field required." }],
        },
        422,
      ),
    );
    const { user } = setup();
    await screen.findByText("Ada Example");

    await user.click(screen.getByRole("button", { name: "Save name" }));

    expect(await screen.findByText("Field required.")).toBeDefined();
  });

  it("reports a missing employee during rename", async () => {
    fetchMock.mockResolvedValueOnce(json(employee));
    fetchMock.mockResolvedValueOnce(
      json(
        { error: { code: "EMPLOYEE_NOT_FOUND", message: "No employee exists." } },
        404,
      ),
    );
    const { user } = setup();
    await screen.findByText("Ada Example");

    await user.click(screen.getByRole("button", { name: "Save name" }));

    expect(await screen.findByText("That employee no longer exists.")).toBeDefined();
  });

  it("reports an unavailable service during rename", async () => {
    fetchMock.mockResolvedValueOnce(json(employee));
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "EMPLOYEE_UNAVAILABLE",
            message: "The employee register could not be read.",
          },
        },
        503,
      ),
    );
    const { user } = setup();
    await screen.findByText("Ada Example");

    await user.click(screen.getByRole("button", { name: "Save name" }));

    expect(
      await screen.findByText(/temporarily unavailable/i),
    ).toBeDefined();
  });

  it("asks the parent to deactivate rather than acting itself", async () => {
    fetchMock.mockResolvedValueOnce(json(employee));
    const { user, onRequestDeactivate } = setup();
    await screen.findByText("Ada Example");

    await user.click(screen.getByRole("button", { name: /deactivate employee/i }));

    expect(onRequestDeactivate).toHaveBeenCalledWith(employee);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
