import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { replaceMock } = vi.hoisted(() => ({
  replaceMock: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: replaceMock,
    push: vi.fn(),
    refresh: vi.fn(),
  }),
}));

import { AuthProvider, useAuth } from "../src/lib/auth/AuthProvider";
import { login as loginRequest } from "../src/lib/api/auth";
import { LoginForm } from "../src/components/auth/LoginForm";

/**
 * Security-focused assertions for the frontend authentication layer.
 *
 * The session is the backend's HttpOnly cookie. These tests pin that the
 * frontend adds no second token store and never fabricates the headers the
 * browser must supply (notably `Origin`, which the login/logout CSRF guard
 * depends on).
 */

const USER = {
  user_id: "user-1",
  username: "ada",
  role: "Admin",
  is_active: true,
};

const AUTH_REQUIRED = {
  error: {
    code: "AUTHENTICATION_REQUIRED",
    message: "Authentication is required.",
  },
};

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function KeysProbe() {
  const auth = useAuth();
  return (
    <>
      <p data-testid="keys">{Object.keys(auth).sort().join(",")}</p>
      <p data-testid="state">{JSON.stringify(auth.user)}</p>
    </>
  );
}

beforeEach(() => {
  replaceMock.mockReset();
  fetchMock.mockReset();
  fetchMock.mockImplementation((input) =>
    String(input).endsWith("/api/v1/auth/me")
      ? Promise.resolve(json(AUTH_REQUIRED, 401))
      : Promise.resolve(json({ user: USER })),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("no second token store", () => {
  it("writes nothing to localStorage, sessionStorage or document.cookie on sign-in", async () => {
    const localSet = vi.spyOn(window.localStorage, "setItem");
    const sessionSet = vi.spyOn(window.sessionStorage, "setItem");

    const user = userEvent.setup();
    render(
      <AuthProvider>
        <LoginForm nextPath="/overview" />
      </AuthProvider>,
    );

    await screen.findByRole("heading", { name: "Sign in" });
    await user.type(screen.getByLabelText(/^Username/), "ada");
    await user.type(screen.getByLabelText(/^Password/), "secret");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/overview");
    });

    expect(localSet).not.toHaveBeenCalled();
    expect(sessionSet).not.toHaveBeenCalled();
    expect(Object.keys(window.localStorage)).toEqual([]);
    expect(Object.keys(window.sessionStorage)).toEqual([]);
    // The session cookie is HttpOnly, so script sees no va_session value.
    expect(document.cookie).not.toContain("va_session");
  });

  it("exposes no token or credential field in the auth state", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json(USER)));
    render(
      <AuthProvider>
        <KeysProbe />
      </AuthProvider>,
    );

    const keys = await screen.findByTestId("keys");
    expect(keys.textContent).toBe(
      "error,notice,refresh,signIn,signOut,status,user",
    );
    const serialized = screen.getByTestId("state").textContent ?? "";
    expect(serialized).not.toContain("token");
    expect(serialized).not.toContain("password");
    expect(serialized).not.toContain("cookie");
  });

  it("returns only the four identity fields from a successful login", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ user: USER }), {
          status: 200,
          headers: {
            "content-type": "application/json",
            // The browser would store this HttpOnly cookie; the client code
            // must not read it or surface it in the resolved value.
            "set-cookie":
              "va_session=secret-session-token; HttpOnly; Path=/api/v1; SameSite=lax",
          },
        }),
      ),
    );

    const result = await loginRequest({ username: "ada", password: "secret" });

    expect(Object.keys(result).sort()).toEqual([
      "is_active",
      "role",
      "user_id",
      "username",
    ]);
    expect(JSON.stringify(result)).not.toContain("secret-session-token");
  });
});

describe("CSRF-relevant request shape", () => {
  it("sends same-origin credentials and never fabricates an Origin header", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json({ user: USER })));

    await loginRequest({ username: "ada", password: "secret" });

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.credentials).toBe("same-origin");
    const headers = init.headers as Record<string, string>;
    // The browser adds Origin on the cross-... same-origin POST; the client
    // must not guess it, because the backend allowlist checks the real value.
    expect(headers.origin).toBeUndefined();
    expect(headers.cookie).toBeUndefined();
    expect(Object.keys(headers).sort()).toEqual(["accept", "content-type"]);
  });
});
