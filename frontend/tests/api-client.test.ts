import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildProxyUrl, requestJson } from "../src/lib/api/client";
import { fetchAllEmployees } from "../src/lib/api/employees";
import { isApiError } from "../src/lib/api/errors";
import { parseEmployeeList } from "../src/lib/api/types";

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

const employeePage = {
  employees: [employee],
  count: 1,
  limit: 100,
  next_after: null,
};

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("URL construction", () => {
  it("always targets the same-origin proxy", () => {
    expect(buildProxyUrl("/api/v1/health")).toBe("/api/backend/api/v1/health");
    const search = new URLSearchParams({ limit: "20" });
    expect(buildProxyUrl("/api/v1/employees", search)).toBe(
      "/api/backend/api/v1/employees?limit=20",
    );
  });

  it("omits the query string when there is nothing to send", () => {
    expect(buildProxyUrl("/api/v1/attendance", new URLSearchParams())).toBe(
      "/api/backend/api/v1/attendance",
    );
  });
});

describe("requestJson", () => {
  it("sends a GET with no body and parses the response", async () => {
    fetchMock.mockResolvedValueOnce(json(employeePage));

    const result = await fetchAllEmployees();

    expect(result).toHaveLength(1);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/backend/api/v1/employees?limit=100");
    expect(init?.method).toBe("GET");
    expect(init?.body).toBeUndefined();
    expect(init?.credentials).toBe("same-origin");
  });

  it("maps a 404 envelope to a typed HTTP error", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        { error: { code: "EMPLOYEE_NOT_FOUND", message: "No employee exists." } },
        404,
      ),
    );

    await expect(
      requestJson({
        path: "/api/v1/employees/missing",
        parse: parseEmployeeList,
      }),
    ).rejects.toMatchObject({
      kind: "http",
      status: 404,
      code: "EMPLOYEE_NOT_FOUND",
      message: "No employee exists.",
    });
  });

  it("preserves a 409 business reason", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "CHECKOUT_REJECTED",
            message: "Refused.",
            reason: "CHECKOUT_COOLDOWN",
          },
        },
        409,
      ),
    );

    await expect(
      requestJson({ path: "/api/v1/attendance/E1/checkout", method: "POST", parse: parseEmployeeList }),
    ).rejects.toMatchObject({ status: 409, reason: "CHECKOUT_COOLDOWN" });
  });

  it("parses a 422 field list", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: { code: "REQUEST_INVALID", message: "Request validation failed." },
          fields: [{ field: "full_name", message: "Field required." }],
        },
        422,
      ),
    );

    const error = await requestJson({
      path: "/api/v1/employees",
      method: "POST",
      body: {},
      parse: parseEmployeeList,
    }).catch((cause: unknown) => cause);

    expect(isApiError(error)).toBe(true);
    if (isApiError(error)) {
      expect(error.status).toBe(422);
      expect(error.fields).toEqual([
        { field: "full_name", message: "Field required." },
      ]);
    }
  });

  it("maps a 503 to a typed error", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "ATTENDANCE_UNAVAILABLE",
            message: "The attendance journal could not be read.",
          },
        },
        503,
      ),
    );

    await expect(
      requestJson({ path: "/api/v1/attendance", parse: parseEmployeeList }),
    ).rejects.toMatchObject({ status: 503, code: "ATTENDANCE_UNAVAILABLE" });
  });

  it("rejects a success status with invalid JSON", async () => {
    fetchMock.mockResolvedValueOnce(new Response("<html>oops</html>", { status: 200 }));

    await expect(
      requestJson({ path: "/api/v1/employees", parse: parseEmployeeList }),
    ).rejects.toMatchObject({ kind: "malformed" });
  });

  it("rejects a success status with an unexpected shape", async () => {
    fetchMock.mockResolvedValueOnce(json({ employees: "not-an-array" }));

    await expect(
      requestJson({ path: "/api/v1/employees", parse: parseEmployeeList }),
    ).rejects.toMatchObject({ kind: "malformed", code: "MALFORMED_RESPONSE" });
  });

  it("falls back to a generic code for a non-JSON error body", async () => {
    fetchMock.mockResolvedValueOnce(new Response("gateway down", { status: 502 }));

    await expect(
      requestJson({ path: "/api/v1/health", parse: parseEmployeeList }),
    ).rejects.toMatchObject({ status: 502, code: "HTTP_ERROR" });
  });

  it("distinguishes a network failure", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    await expect(
      requestJson({ path: "/api/v1/health", parse: parseEmployeeList }),
    ).rejects.toMatchObject({ kind: "network", code: "NETWORK_UNAVAILABLE" });
  });

  it("rethrows the abort error instead of reporting a network failure", async () => {
    const controller = new AbortController();
    controller.abort();
    const abortError = new Error("aborted");
    abortError.name = "AbortError";
    fetchMock.mockRejectedValueOnce(abortError);

    const error = await requestJson({
      path: "/api/v1/attendance",
      signal: controller.signal,
      parse: parseEmployeeList,
    }).catch((cause: unknown) => cause);

    expect(isApiError(error)).toBe(false);
    expect((error as Error).name).toBe("AbortError");
  });

  it("passes the abort signal through to fetch", async () => {
    const controller = new AbortController();
    fetchMock.mockResolvedValueOnce(json(employeePage));

    await requestJson({
      path: "/api/v1/employees",
      signal: controller.signal,
      parse: parseEmployeeList,
    });

    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
  });
});
