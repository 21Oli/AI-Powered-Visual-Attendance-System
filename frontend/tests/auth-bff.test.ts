import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  GET,
  POST,
} from "../src/app/api/v1/auth/[action]/route";

const BACKEND = "http://backend.test:8000";
const ORIGIN = "https://attendance.test.invalid";

const fetchMock = vi.fn<typeof fetch>();

const handlers = { GET, POST };

function call(
  method: "GET" | "POST",
  action: string,
  options: {
    body?: string;
    headers?: Record<string, string>;
  } = {},
) {
  const request = new Request(`http://localhost:3000/api/v1/auth/${action}`, {
    method,
    body: options.body,
    headers: options.headers,
  });
  return handlers[method](request, {
    params: Promise.resolve({ action }),
  });
}

function upstreamJson(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

async function bodyOf(response: Response): Promise<Record<string, unknown>> {
  return (await response.json()) as Record<string, unknown>;
}

const USER = {
  user_id: "user-1",
  username: "ada",
  role: "Admin",
  is_active: true,
};

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(upstreamJson(USER));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("BACKEND_BASE_URL", BACKEND);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("/api/v1/auth BFF boundary", () => {
  it("serves GET /auth/me from the backend with only the session cookie", async () => {
    const response = await call("GET", "me", {
      headers: { cookie: "theme=dark; va_session=the-token" },
    });

    expect(response.status).toBe(200);
    expect(await bodyOf(response)).toEqual(USER);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${BACKEND}/api/v1/auth/me`);
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("GET");
    expect(init.headers).toMatchObject({ cookie: "va_session=the-token" });
  });

  it("serves POST /auth/login and preserves the Set-Cookie it receives", async () => {
    fetchMock.mockResolvedValueOnce(
      upstreamJson(
        { user: USER },
        200,
        {
          "set-cookie":
            "va_session=new-session-token; HttpOnly; Path=/api/v1; SameSite=lax; Max-Age=43200",
        },
      ),
    );

    const response = await call("POST", "login", {
      body: JSON.stringify({ username: "ada", password: "secret" }),
      headers: { origin: ORIGIN },
    });

    expect(response.status).toBe(200);
    expect(await bodyOf(response)).toEqual({ user: USER });
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${BACKEND}/api/v1/auth/login`);
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.headers).toMatchObject({
      origin: ORIGIN,
      "content-type": "application/json",
    });
    const cookie = response.headers.get("set-cookie") ?? "";
    expect(cookie).toContain("va_session=new-session-token");
    expect(cookie).toContain("Path=/api/v1");
    expect(cookie).toContain("HttpOnly");
    expect(cookie.toLowerCase()).toContain("samesite=lax");
  });

  it("serves POST /auth/logout as a bodyless 204 with the clearing cookie", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(null, {
        status: 204,
        headers: {
          "set-cookie":
            'va_session=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/api/v1; HttpOnly; SameSite=lax',
        },
      }),
    );

    const response = await call("POST", "logout", {
      headers: { origin: ORIGIN, cookie: "va_session=the-token" },
    });

    expect(response.status).toBe(204);
    expect(await response.text()).toBe("");
    expect(response.headers.get("set-cookie")).toContain("Max-Age=0");
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${BACKEND}/api/v1/auth/logout`);
  });

  it("never exposes an unlisted action, even though the segment is dynamic", async () => {
    for (const action of ["password-reset", "refresh", "sessions", "me/extra"]) {
      const response = await call("POST", action, {
        body: JSON.stringify({ anything: true }),
        headers: { origin: ORIGIN },
      });
      expect([404, 405]).toContain(response.status);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects the wrong method on each auth path", async () => {
    expect((await call("GET", "login")).status).toBe(405);
    expect((await call("POST", "me")).status).toBe(405);
    expect((await call("GET", "logout")).status).toBe(405);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("exposes only GET and POST handlers", async () => {
    const routeModule: Record<string, unknown> = await import(
      "../src/app/api/v1/auth/[action]/route"
    );
    expect(routeModule.DELETE).toBeUndefined();
    expect(routeModule.PUT).toBeUndefined();
    expect(routeModule.PATCH).toBeUndefined();
  });

  it("returns a sanitized 503 when the backend origin is unconfigured", async () => {
    vi.stubEnv("BACKEND_BASE_URL", "");

    const response = await call("GET", "me", {
      headers: { cookie: "va_session=the-token" },
    });

    expect(response.status).toBe(503);
    expect((await bodyOf(response)).error).toMatchObject({
      code: "BACKEND_NOT_CONFIGURED",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never echoes the backend origin or the session cookie in the body", async () => {
    fetchMock.mockResolvedValueOnce(
      upstreamJson(
        { error: { code: "AUTHENTICATION_REQUIRED", message: "Authentication is required." } },
        401,
      ),
    );

    const response = await call("GET", "me", {
      headers: { cookie: "va_session=the-token" },
    });
    const text = await response.text();

    expect(response.status).toBe(401);
    expect(text).not.toContain(BACKEND);
    expect(text).not.toContain("the-token");
  });
});
