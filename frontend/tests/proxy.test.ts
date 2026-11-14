import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET, PATCH, POST } from "../src/app/api/backend/[...path]/route";

const BACKEND = "http://backend.test:8000";

type Handler = typeof GET;

const handlers: Record<"GET" | "POST" | "PATCH", Handler> = { GET, POST, PATCH };

const fetchMock = vi.fn<typeof fetch>();

function call(
  method: "GET" | "POST" | "PATCH",
  path: string,
  options: {
    body?: string;
    query?: string;
    headers?: Record<string, string>;
  } = {},
) {
  const url = `http://localhost:3000/api/backend/${path}${
    options.query === undefined ? "" : `?${options.query}`
  }`;
  const request = new Request(url, {
    method,
    body: options.body,
    headers: options.headers,
  });
  const segments = path.split("/");
  return handlers[method](request, {
    params: Promise.resolve({ path: segments }),
  });
}

function upstreamJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function bodyOf(response: Response): Promise<Record<string, unknown>> {
  return (await response.json()) as Record<string, unknown>;
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(upstreamJson({ status: "ok" }));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("BACKEND_BASE_URL", BACKEND);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("proxy allowlist", () => {
  const approved: ReadonlyArray<{
    method: "GET" | "POST" | "PATCH";
    path: string;
    query?: string;
    body?: string;
    upstream: string;
    status: number;
  }> = [
    { method: "GET", path: "api/v1/health", upstream: `${BACKEND}/api/v1/health`, status: 200 },
    {
      method: "POST",
      path: "api/v1/employees",
      body: JSON.stringify({ employee_id: "E1", full_name: "A Name" }),
      upstream: `${BACKEND}/api/v1/employees`,
      status: 201,
    },
    {
      method: "GET",
      path: "api/v1/employees",
      query: "limit=100",
      upstream: `${BACKEND}/api/v1/employees?limit=100`,
      status: 200,
    },
    {
      method: "GET",
      path: "api/v1/employees/EMP-1",
      upstream: `${BACKEND}/api/v1/employees/EMP-1`,
      status: 200,
    },
    {
      method: "PATCH",
      path: "api/v1/employees/EMP-1",
      body: JSON.stringify({ full_name: "New Name" }),
      upstream: `${BACKEND}/api/v1/employees/EMP-1`,
      status: 200,
    },
    {
      method: "POST",
      path: "api/v1/employees/EMP-1/deactivate",
      upstream: `${BACKEND}/api/v1/employees/EMP-1/deactivate`,
      status: 200,
    },
    {
      method: "GET",
      path: "api/v1/attendance",
      upstream: `${BACKEND}/api/v1/attendance`,
      status: 200,
    },
    {
      method: "POST",
      path: "api/v1/attendance/EMP-1/checkout",
      upstream: `${BACKEND}/api/v1/attendance/EMP-1/checkout`,
      status: 201,
    },
  ];

  it.each(approved)("forwards $method /$path", async (entry) => {
    fetchMock.mockResolvedValueOnce(upstreamJson({ ok: true }, entry.status));

    const response = await call(entry.method, entry.path, {
      body: entry.body,
      query: entry.query,
    });

    expect(response.status).toBe(entry.status);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe(entry.upstream);
    expect((init as RequestInit | undefined)?.method).toBe(entry.method);
  });

  it("refuses an unapproved path", async () => {
    const response = await call("GET", "api/v1/admin");

    expect(response.status).toBe(404);
    expect((await bodyOf(response)).error).toEqual({
      code: "PROXY_ROUTE_NOT_ALLOWED",
      message: "That backend route is not available through this dashboard.",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("refuses an unapproved method on an approved path", async () => {
    const response = await call("POST", "api/v1/health");

    expect(response.status).toBe(405);
    expect(response.headers.get("allow")).toBe("GET");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not expose a DELETE handler at all", async () => {
    const routeModule: Record<string, unknown> = await import(
      "../src/app/api/backend/[...path]/route"
    );
    expect(routeModule.DELETE).toBeUndefined();
    expect(routeModule.PUT).toBeUndefined();
  });
});

describe("proxy path and identifier safety", () => {
  it.each([
    ["api/v1/employees/..", "traversal"],
    ["api/v1/employees/.", "current directory"],
    ["api/v1/employees/a/b", "extra segment"],
    ["api/v1/employees/a%3Fb", "query marker"],
  ])("rejects %s (%s)", async (path) => {
    const response = await call("GET", path);
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an identifier that is blank or padded", async () => {
    for (const path of ["api/v1/employees/ ", "api/v1/employees/ EMP-1"]) {
      const response = await call("GET", path);
      expect(response.status).toBe(404);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("re-encodes a decoded identifier exactly once when forwarding", async () => {
    // Next.js hands the handler decoded segments, so this is what a request for
    // /employees/EMP%201 actually looks like.
    await call("GET", "api/v1/employees/EMP 1");

    const [url] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe(`${BACKEND}/api/v1/employees/EMP%201`);
  });

  it("accepts an identifier containing unicode", async () => {
    await call("GET", "api/v1/employees/Renée");

    const [url] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe(`${BACKEND}/api/v1/employees/Ren%C3%A9e`);
  });
});

describe("proxy query handling", () => {
  it("forwards only the supported employee paging parameters", async () => {
    await call("GET", "api/v1/employees", { query: "after=EMP-9&limit=50" });

    const [url] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe(`${BACKEND}/api/v1/employees?after=EMP-9&limit=50`);
  });

  it("rejects an unsupported query parameter", async () => {
    const response = await call("GET", "api/v1/employees", { query: "sort=name" });

    expect(response.status).toBe(400);
    expect((await bodyOf(response)).error).toMatchObject({
      code: "PROXY_REQUEST_INVALID",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a query on a route that accepts none", async () => {
    const response = await call("GET", "api/v1/attendance", { query: "limit=10" });

    expect(response.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["0", "-1", "abc", "100.5", "10000"])(
    "rejects page size %s",
    async (limit) => {
      const response = await call("GET", "api/v1/employees", { query: `limit=${limit}` });
      expect(response.status).toBe(400);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("rejects an unsafe pagination cursor", async () => {
    const response = await call("GET", "api/v1/employees", { query: "after=a%2Fb" });
    expect(response.status).toBe(400);
  });
});

describe("proxy request bodies", () => {
  it("forwards the JSON body for approved writes", async () => {
    const payload = JSON.stringify({ employee_id: "E1", full_name: "A Name" });
    await call("POST", "api/v1/employees", { body: payload });

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect((init as RequestInit).body).toBe(payload);
    expect((init as RequestInit).headers).toMatchObject({
      "content-type": "application/json",
    });
  });

  it("never sends a body for checkout, even if one is supplied", async () => {
    await call("POST", "api/v1/attendance/EMP-1/checkout", {
      body: JSON.stringify({ occurred_at: "2026-01-01T00:00:00Z" }),
    });

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect((init as RequestInit).body).toBeUndefined();
  });

  it("never sends a body for reads", async () => {
    await call("GET", "api/v1/employees/EMP-1");

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect((init as RequestInit).body).toBeUndefined();
  });

  it("rejects an oversized body", async () => {
    const response = await call("POST", "api/v1/employees", {
      body: JSON.stringify({ full_name: "x".repeat(5000) }),
    });

    expect(response.status).toBe(413);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("proxy upstream behaviour", () => {
  it("preserves an upstream error status and envelope", async () => {
    fetchMock.mockResolvedValueOnce(
      upstreamJson(
        {
          error: {
            code: "CHECKOUT_REJECTED",
            message: "The check-out was refused.",
            reason: "CHECKOUT_COOLDOWN",
          },
        },
        409,
      ),
    );

    const response = await call("POST", "api/v1/attendance/EMP-1/checkout");

    expect(response.status).toBe(409);
    const body = await bodyOf(response);
    expect(body.error).toMatchObject({ code: "CHECKOUT_REJECTED", reason: "CHECKOUT_COOLDOWN" });
  });

  it("does not follow a redirect from the backend", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(null, { status: 302, headers: { location: "http://evil.test/" } }),
    );

    const response = await call("GET", "api/v1/health");

    expect(response.status).toBe(502);
    expect((await bodyOf(response)).error).toMatchObject({
      code: "BACKEND_REDIRECT_REFUSED",
    });
    expect(fetchMock.mock.calls[0]?.[1]?.redirect).toBe("manual");
  });

  it("returns a sanitized gateway error when the backend is unreachable", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("connect ECONNREFUSED 127.0.0.1:8000"));

    const response = await call("GET", "api/v1/health");
    const text = JSON.stringify(await bodyOf(response));

    expect(response.status).toBe(502);
    expect(text).toContain("BACKEND_UNAVAILABLE");
    expect(text).not.toContain("ECONNREFUSED");
    expect(text).not.toContain(BACKEND);
  });

  it("returns 504 when the backend times out", async () => {
    const timeout = new Error("timed out");
    timeout.name = "TimeoutError";
    fetchMock.mockRejectedValueOnce(timeout);

    const response = await call("GET", "api/v1/health");

    expect(response.status).toBe(504);
    expect((await bodyOf(response)).error).toMatchObject({ code: "BACKEND_TIMEOUT" });
  });

  it("never caches authenticated operational data", async () => {
    const response = await call("GET", "api/v1/attendance");
    expect(response.headers.get("cache-control")).toBe("no-store");
  });
});

describe("proxy configuration", () => {
  it("refuses to run without a configured backend", async () => {
    vi.stubEnv("BACKEND_BASE_URL", "");

    const response = await call("GET", "api/v1/health");

    expect(response.status).toBe(503);
    expect((await bodyOf(response)).error).toMatchObject({ code: "BACKEND_NOT_CONFIGURED" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    "not a url",
    "ftp://backend.test",
    "http://user:pass@backend.test",
    "http://backend.test/api",
    "http://backend.test/?x=1",
    "http://backend.test/#frag",
  ])("rejects the configured value %s", async (value) => {
    vi.stubEnv("BACKEND_BASE_URL", value);

    const response = await call("GET", "api/v1/health");

    expect(response.status).toBe(503);
    expect((await bodyOf(response)).error).toMatchObject({
      code: "BACKEND_CONFIGURATION_INVALID",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never returns the configured backend origin to the client", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("boom"));

    const response = await call("GET", "api/v1/employees");
    const text = await response.text();

    expect(text).not.toContain(BACKEND);
    expect(text).not.toContain("stack");
  });
});

describe("proxy login route (P5.4.5)", () => {
  const ORIGIN = "https://attendance.test.invalid";
  const payload = JSON.stringify({ username: "ada", password: "secret-value" });

  it("allows only POST /api/v1/auth/login and forwards its JSON body", async () => {
    await call("POST", "api/v1/auth/login", {
      body: payload,
      headers: { origin: ORIGIN },
    });

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(String(url)).toBe(`${BACKEND}/api/v1/auth/login`);
    expect((init as RequestInit).body).toBe(payload);
    expect((init as RequestInit).headers).toMatchObject({
      "content-type": "application/json",
    });

    const refused = await call("GET", "api/v1/auth/login");
    expect(refused.status).toBe(405);
  });

  it("forwards the browser Origin unchanged for login only", async () => {
    await call("POST", "api/v1/auth/login", {
      body: payload,
      headers: { origin: ORIGIN },
    });

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect((init as RequestInit).headers).toMatchObject({ origin: ORIGIN });
  });

  it("never synthesizes an Origin when the browser sent none", async () => {
    await call("POST", "api/v1/auth/login", { body: payload });

    const [, init] = fetchMock.mock.calls[0] ?? [];
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers.origin).toBeUndefined();
  });

  it("does not forward Origin for the business routes", async () => {
    await call("POST", "api/v1/employees", {
      body: JSON.stringify({ employee_id: "E1", full_name: "A Name" }),
      headers: { origin: ORIGIN },
    });

    const [, init] = fetchMock.mock.calls[0] ?? [];
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers.origin).toBeUndefined();
  });

  it("never forwards arbitrary client headers", async () => {
    await call("POST", "api/v1/auth/login", {
      body: payload,
      headers: {
        origin: ORIGIN,
        authorization: "Bearer client-token",
        cookie: "va_session=client-value",
        "x-forwarded-host": "attendance.test.invalid",
        "x-forwarded-proto": "https",
      },
    });

    const [, init] = fetchMock.mock.calls[0] ?? [];
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(Object.keys(headers).sort()).toEqual(["accept", "content-type", "origin"]);
    expect(JSON.stringify(headers)).not.toContain("client-token");
    expect(JSON.stringify(headers)).not.toContain("client-value");
  });

  it("drops the backend Set-Cookie (BFF cookie integration is later)", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ user: { user_id: "u1" } }), {
        status: 200,
        headers: {
          "content-type": "application/json",
          "set-cookie": "va_session=token-value; HttpOnly; Path=/api/v1",
        },
      }),
    );

    const response = await call("POST", "api/v1/auth/login", {
      body: payload,
      headers: { origin: ORIGIN },
    });

    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(await response.text()).not.toContain("token-value");
  });
});
