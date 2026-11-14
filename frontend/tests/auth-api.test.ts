import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchCurrentUser, login, logout } from "../src/lib/api/auth";
import { isApiError } from "../src/lib/api/errors";

const fetchMock = vi.fn<typeof fetch>();

const USER = {
  user_id: "user-1",
  username: "ada",
  role: "Admin",
  is_active: true,
} as const;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function noContent(status = 204): Response {
  return new Response(null, { status });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("login", () => {
  it("posts only the credentials to the same-origin auth path", async () => {
    fetchMock.mockResolvedValueOnce(json({ user: USER }));

    const result = await login({ username: "ada", password: "secret-value" });

    expect(result).toEqual(USER);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/v1/auth/login");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      username: "ada",
      password: "secret-value",
    });
    expect(init?.credentials).toBe("same-origin");
    expect(init?.cache).toBe("no-store");
  });

  it("never returns or stores a session token", async () => {
    fetchMock.mockResolvedValueOnce(json({ user: USER }));

    const result = await login({ username: "ada", password: "secret-value" });
    const serialized = JSON.stringify(result);

    expect(Object.keys(result).sort()).toEqual([
      "is_active",
      "role",
      "user_id",
      "username",
    ]);
    expect(serialized).not.toContain("va_session");
    expect(serialized).not.toContain("session_token");
    expect(serialized).not.toContain("secret-value");
  });

  it.each([
    [401, "AUTHENTICATION_REQUIRED"],
    [403, "ORIGIN_NOT_ALLOWED"],
    [503, "DATABASE_UNAVAILABLE"],
  ])("maps a %s rejection to a typed ApiError", async (status, code) => {
    fetchMock.mockResolvedValueOnce(
      json({ error: { code, message: "Refused." } }, status),
    );

    await expect(
      login({ username: "ada", password: "secret-value" }),
    ).rejects.toMatchObject({
      kind: "http",
      status,
      code,
    });
  });

  it("rejects an unexpected login body instead of coercing it", async () => {
    fetchMock.mockResolvedValueOnce(json({ user: { user_id: "u1" } }));

    await expect(
      login({ username: "ada", password: "secret-value" }),
    ).rejects.toMatchObject({ kind: "malformed" });
  });

  it("reports a network failure as a typed network error", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("offline"));

    await expect(
      login({ username: "ada", password: "secret-value" }),
    ).rejects.toMatchObject({ kind: "network" });
  });
});

describe("fetchCurrentUser", () => {
  it("gets the four-field identity from the same-origin auth path", async () => {
    fetchMock.mockResolvedValueOnce(json(USER));

    const result = await fetchCurrentUser();

    expect(result).toEqual(USER);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/v1/auth/me");
    expect(init?.method).toBe("GET");
    expect(init?.body).toBeUndefined();
    expect(init?.credentials).toBe("same-origin");
  });

  it("preserves a 401 as a typed error", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        { error: { code: "AUTHENTICATION_REQUIRED", message: "Authentication is required." } },
        401,
      ),
    );

    await expect(fetchCurrentUser()).rejects.toMatchObject({
      kind: "http",
      status: 401,
      code: "AUTHENTICATION_REQUIRED",
    });
  });

  it("preserves a sanitized 503", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "DATABASE_UNAVAILABLE",
            message: "Database is temporarily unavailable.",
          },
        },
        503,
      ),
    );

    await expect(fetchCurrentUser()).rejects.toMatchObject({
      status: 503,
      code: "DATABASE_UNAVAILABLE",
    });
  });

  it("refuses an unrecognized role or extra identity shape", async () => {
    fetchMock.mockResolvedValueOnce(json({ ...USER, role: "SuperAdmin" }));
    await expect(fetchCurrentUser()).rejects.toMatchObject({ kind: "malformed" });

    fetchMock.mockResolvedValueOnce(json({ ...USER, is_active: "yes" }));
    await expect(fetchCurrentUser()).rejects.toMatchObject({ kind: "malformed" });
  });

  it("does not include credential material in the parsed value", async () => {
    fetchMock.mockResolvedValueOnce(json(USER));
    const result = await fetchCurrentUser();
    expect(JSON.stringify(result)).not.toMatch(
      /va_session|session_token|password_hash|argon2/,
    );
  });
});

describe("logout", () => {
  it("posts to the same-origin path and accepts a bodyless 204", async () => {
    fetchMock.mockResolvedValueOnce(noContent());

    await expect(logout()).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/v1/auth/logout");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeUndefined();
    expect(init?.credentials).toBe("same-origin");
  });

  it("throws instead of reporting success when the backend fails", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "DATABASE_UNAVAILABLE",
            message: "Database is temporarily unavailable.",
          },
        },
        503,
      ),
    );

    const failure = await logout().catch((error: unknown) => error);
    expect(isApiError(failure)).toBe(true);
    expect(failure).toMatchObject({ status: 503, code: "DATABASE_UNAVAILABLE" });
  });

  it("throws a typed error for a 403 origin rejection", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        { error: { code: "ORIGIN_NOT_ALLOWED", message: "The request origin is not allowed." } },
        403,
      ),
    );

    await expect(logout()).rejects.toMatchObject({
      status: 403,
      code: "ORIGIN_NOT_ALLOWED",
    });
  });

  it("reports a network failure rather than a silent sign-out", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("offline"));

    await expect(logout()).rejects.toMatchObject({ kind: "network" });
  });
});
