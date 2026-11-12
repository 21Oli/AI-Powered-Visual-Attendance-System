import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createEmployee,
  deactivateEmployee,
  fetchEmployeeById,
  renameEmployee,
} from "../src/lib/api/employees";

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

function firstInit(): RequestInit | undefined {
  return fetchMock.mock.calls[0]?.[1];
}

function firstBody(): Record<string, unknown> {
  const body = firstInit()?.body;
  if (typeof body !== "string") {
    throw new Error("Expected a JSON request body.");
  }
  return JSON.parse(body) as Record<string, unknown>;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("employee api client", () => {
  it("creates an employee with the three contracted fields", async () => {
    fetchMock.mockResolvedValueOnce(json(employee, 201));

    const created = await createEmployee({
      employee_id: "EMP-1",
      full_name: "Ada Example",
    });

    expect(created).toEqual(employee);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/backend/api/v1/employees");
    expect(firstInit()?.method).toBe("POST");
    expect(firstBody()).toEqual({
      employee_id: "EMP-1",
      full_name: "Ada Example",
      is_active: true,
    });
    // The client never supplies the application-authoritative timestamp.
    expect(Object.keys(firstBody()).sort()).toEqual([
      "employee_id",
      "full_name",
      "is_active",
    ]);
  });

  it("never normalises the submitted identifier", async () => {
    fetchMock.mockResolvedValueOnce(json({ ...employee, employee_id: "0007" }, 201));

    await createEmployee({ employee_id: "0007", full_name: "Ada Example" });

    expect(firstBody().employee_id).toBe("0007");
  });

  it("reads one employee by an encoded path segment", async () => {
    fetchMock.mockResolvedValueOnce(json(employee));

    const found = await fetchEmployeeById("EMP 1");

    expect(found).toEqual(employee);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/backend/api/v1/employees/EMP%201",
    );
    expect(firstInit()?.method).toBe("GET");
    expect(firstInit()?.body).toBeUndefined();
  });

  it("renames with full_name only", async () => {
    fetchMock.mockResolvedValueOnce(json({ ...employee, full_name: "Ada Lovelace" }));

    const updated = await renameEmployee("EMP-1", "Ada Lovelace");

    expect(updated.full_name).toBe("Ada Lovelace");
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/backend/api/v1/employees/EMP-1",
    );
    expect(firstInit()?.method).toBe("PATCH");
    expect(firstBody()).toEqual({ full_name: "Ada Lovelace" });
  });

  it("deactivates with a POST and no body", async () => {
    fetchMock.mockResolvedValueOnce(json({ ...employee, is_active: false }));

    const updated = await deactivateEmployee("EMP-1");

    expect(updated.is_active).toBe(false);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/backend/api/v1/employees/EMP-1/deactivate",
    );
    expect(firstInit()?.method).toBe("POST");
    expect(firstInit()?.body).toBeUndefined();
    expect(firstInit()?.headers).toEqual({ accept: "application/json" });
  });

  it("propagates a duplicate business error", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        { error: { code: "EMPLOYEE_EXISTS", message: "Employee already exists." } },
        409,
      ),
    );

    await expect(
      createEmployee({ employee_id: "EMP-1", full_name: "Ada Example" }),
    ).rejects.toMatchObject({
      kind: "http",
      status: 409,
      code: "EMPLOYEE_EXISTS",
    });
  });

  it("rejects a malformed success body", async () => {
    fetchMock.mockResolvedValueOnce(json({ employee_id: "EMP-1" }));

    await expect(fetchEmployeeById("EMP-1")).rejects.toMatchObject({
      kind: "malformed",
      code: "MALFORMED_RESPONSE",
    });
  });

  it("passes an abort signal through", async () => {
    const controller = new AbortController();
    fetchMock.mockResolvedValueOnce(json(employee));

    await fetchEmployeeById("EMP-1", controller.signal);

    expect(firstInit()?.signal).toBe(controller.signal);
  });
});
