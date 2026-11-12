import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CreateEmployeeForm } from "../src/components/employees/CreateEmployeeForm";

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const created = {
  employee_id: "EMP-1",
  full_name: "Ada Example",
  is_active: true,
  created_at: "2026-01-01T00:00:00+00:00",
};

function setup() {
  const onCreated = vi.fn();
  const onCancel = vi.fn();
  const user = userEvent.setup();
  render(<CreateEmployeeForm onCreated={onCreated} onCancel={onCancel} />);
  return { user, onCreated, onCancel };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("create employee form", () => {
  it("requires both fields before calling the API", async () => {
    const { user } = setup();

    await user.click(screen.getByRole("button", { name: "Create employee" }));

    expect(screen.getByText("Enter an employee ID.")).toBeDefined();
    expect(screen.getByText("Enter the employee's full name.")).toBeDefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an identifier the proxy could not address", async () => {
    const { user } = setup();

    await user.type(screen.getByLabelText(/employee id/i), "EMP/1");
    await user.type(screen.getByLabelText(/full name/i), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("control characters");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an identifier with surrounding whitespace without trimming it", async () => {
    const { user } = setup();

    await user.type(screen.getByLabelText(/employee id/i), "EMP-1 ");
    await user.type(screen.getByLabelText(/full name/i), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    expect(screen.getByText(/remove the spaces/i)).toBeDefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("creates the employee and reports it upward", async () => {
    fetchMock.mockResolvedValueOnce(json(created, 201));
    const { user, onCreated } = setup();

    await user.type(screen.getByLabelText(/employee id/i), "EMP-1");
    await user.type(screen.getByLabelText(/full name/i), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    await waitFor(() => {
      expect(onCreated).toHaveBeenCalledWith(created);
    });

    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.method).toBe("POST");
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    expect(body).toEqual({
      employee_id: "EMP-1",
      full_name: "Ada Example",
      is_active: true,
    });
    expect(body.created_at).toBeUndefined();
  });

  it("reports a duplicate identifier on the id field", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        { error: { code: "EMPLOYEE_EXISTS", message: "Employee already exists." } },
        409,
      ),
    );
    const { user, onCreated } = setup();

    await user.type(screen.getByLabelText(/employee id/i), "EMP-1");
    await user.type(screen.getByLabelText(/full name/i), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    expect(
      await screen.findByText("An employee with this ID already exists."),
    ).toBeDefined();
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("maps a pydantic field path onto the name field", async () => {
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

    await user.type(screen.getByLabelText(/employee id/i), "EMP-1");
    await user.type(screen.getByLabelText(/full name/i), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    expect(await screen.findByText("Field required.")).toBeDefined();
  });

  it("keeps a validation error it cannot map", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: { code: "REQUEST_INVALID", message: "Request validation failed." },
          fields: [
            { field: "body.is_active", message: "Extra inputs are not permitted." },
          ],
        },
        422,
      ),
    );
    const { user } = setup();

    await user.type(screen.getByLabelText(/employee id/i), "EMP-1");
    await user.type(screen.getByLabelText(/full name/i), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("body.is_active");
    expect(alert.textContent).toContain("Extra inputs are not permitted.");
  });

  it("distinguishes a proxy refusal from backend validation", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "PROXY_REQUEST_INVALID",
            message: "The request body is too large.",
          },
        },
        413,
      ),
    );
    const { user } = setup();

    await user.type(screen.getByLabelText(/employee id/i), "EMP-1");
    await user.type(screen.getByLabelText(/full name/i), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/rejected by the dashboard proxy/i);
    expect(screen.queryByText(/field required/i)).toBeNull();
  });

  it("reports a network failure", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const { user } = setup();

    await user.type(screen.getByLabelText(/employee id/i), "EMP-1");
    await user.type(screen.getByLabelText(/full name/i), "Ada Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/could not reach/i);
  });

  it("cancels without calling the API", async () => {
    const { user, onCancel } = setup();

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
