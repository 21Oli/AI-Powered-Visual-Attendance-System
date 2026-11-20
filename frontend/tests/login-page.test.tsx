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
import { LoginForm } from "../src/components/auth/LoginForm";
import {
  LOGIN_INVALID_CREDENTIALS_MESSAGE,
  LOGIN_NETWORK_MESSAGE,
  SESSION_EXPIRED_MESSAGE,
} from "../src/lib/auth/messages";

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

function noSession(): Promise<Response> {
  return Promise.resolve(json(AUTH_REQUIRED, 401));
}

function loginCalls() {
  return fetchMock.mock.calls.filter(
    ([input]) => String(input) === "/api/v1/auth/login",
  );
}

function renderLogin(nextPath: string | null = "/employees") {
  return render(
    <AuthProvider>
      <LoginForm nextPath={nextPath} />
    </AuthProvider>,
  );
}

async function waitForForm() {
  return screen.findByRole("heading", { level: 1, name: "Sign in" });
}

beforeEach(() => {
  replaceMock.mockReset();
  fetchMock.mockReset();
  fetchMock.mockImplementation((input) => {
    const url = String(input);
    return url.endsWith("/api/v1/auth/me")
      ? noSession()
      : Promise.resolve(json({ user: USER }));
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("login page", () => {
  it("signs in and returns to the validated destination", async () => {
    const user = userEvent.setup();
    renderLogin("/employees");
    await waitForForm();

    await user.type(screen.getByLabelText(/^Username/), "ada");
    await user.type(screen.getByLabelText(/^Password/), "correct-horse");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/employees");
    });

    const [call] = loginCalls();
    expect(call).toBeDefined();
    const init = call?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      username: "ada",
      password: "correct-horse",
    });
  });

  it("sends the username only once and never a password it was not given", async () => {
    const user = userEvent.setup();
    renderLogin();
    await waitForForm();

    await user.type(screen.getByLabelText(/^Username/), "  ada  ");
    await user.type(screen.getByLabelText(/^Password/), " spaced-pass ");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(loginCalls()).toHaveLength(1);
    });
    const body = JSON.parse(String((loginCalls()[0]?.[1] as RequestInit).body));
    // Whitespace-padded username is trimmed; the password is passed untouched.
    expect(body).toEqual({ username: "ada", password: " spaced-pass " });
  });

  it("reports invalid credentials with a fixed, sanitized message", async () => {
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      return url.endsWith("/api/v1/auth/me")
        ? noSession()
        : Promise.resolve(json(AUTH_REQUIRED, 401));
    });
    const user = userEvent.setup();
    renderLogin();
    await waitForForm();

    await user.type(screen.getByLabelText(/^Username/), "ada");
    await user.type(screen.getByLabelText(/^Password/), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(LOGIN_INVALID_CREDENTIALS_MESSAGE)).toBeDefined();
    expect(replaceMock).not.toHaveBeenCalled();
    // The form is usable again after a failure.
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDefined();
  });

  it("requires both fields before contacting the backend", async () => {
    const user = userEvent.setup();
    renderLogin();
    await waitForForm();

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByText("Enter your username.")).toBeDefined();
    expect(screen.getByText("Enter your password.")).toBeDefined();
    expect(loginCalls()).toHaveLength(0);
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("reports an unreachable backend without treating it as bad credentials", async () => {
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      return url.endsWith("/api/v1/auth/me")
        ? noSession()
        : Promise.reject(new TypeError("connect ECONNREFUSED 127.0.0.1:8000"));
    });
    const user = userEvent.setup();
    renderLogin();
    await waitForForm();

    await user.type(screen.getByLabelText(/^Username/), "ada");
    await user.type(screen.getByLabelText(/^Password/), "secret");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(LOGIN_NETWORK_MESSAGE)).toBeDefined();
    expect(document.body.textContent).not.toContain("ECONNREFUSED");
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("never renders backend internals from an error envelope", async () => {
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      return url.endsWith("/api/v1/auth/me")
        ? noSession()
        : Promise.resolve(
            json(
              {
                error: {
                  code: "INTERNAL_ERROR",
                  message:
                    'psycopg2.OperationalError: password authentication failed for user "postgres" at 10.0.0.5',
                },
              },
              500,
            ),
          );
    });
    const user = userEvent.setup();
    renderLogin();
    await waitForForm();

    await user.type(screen.getByLabelText(/^Username/), "ada");
    await user.type(screen.getByLabelText(/^Password/), "secret");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(
      await screen.findByText("Sign-in could not be completed. Try again."),
    ).toBeDefined();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("psycopg2");
    expect(text).not.toContain("postgres");
    expect(text).not.toContain("10.0.0.5");
  });

  it("prevents duplicate submissions while the first one is pending", async () => {
    let releaseLogin: ((response: Response) => void) | undefined;
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/api/v1/auth/me")) {
        return noSession();
      }
      return new Promise<Response>((resolve) => {
        releaseLogin = resolve;
      });
    });
    const user = userEvent.setup();
    renderLogin();
    await waitForForm();

    await user.type(screen.getByLabelText(/^Username/), "ada");
    await user.type(screen.getByLabelText(/^Password/), "secret");

    const submit = screen.getByRole("button", { name: "Sign in" });
    await user.click(submit);
    await user.click(submit);

    expect(loginCalls()).toHaveLength(1);
    expect(await screen.findByRole("button", { name: "Signing in…" })).toBeDefined();

    releaseLogin?.(json({ user: USER }));
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/employees");
    });
  });

  it("shows and hides the password on request", async () => {
    const user = userEvent.setup();
    renderLogin();
    await waitForForm();

    const password = screen.getByLabelText(/^Password/) as HTMLInputElement;
    expect(password.type).toBe("password");
    expect(
      screen.getByRole("button", { name: "Show password" }).getAttribute(
        "aria-pressed",
      ),
    ).toBe("false");

    await user.click(screen.getByRole("button", { name: "Show password" }));

    expect(password.type).toBe("text");
    expect(
      screen.getByRole("button", { name: "Hide password" }).getAttribute(
        "aria-pressed",
      ),
    ).toBe("true");
  });

  it("keeps a first visit neutral: no expiry claim", async () => {
    renderLogin();
    await waitForForm();

    expect(screen.queryByText(SESSION_EXPIRED_MESSAGE)).toBeNull();
    expect(
      screen.getByText(/Access is limited to authorized staff/i),
    ).toBeDefined();
  });

  it("explains an expiry only after this tab had a session", async () => {
    let checks = 0;
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (!url.endsWith("/api/v1/auth/me")) {
        return Promise.resolve(json({ user: USER }));
      }
      checks += 1;
      return Promise.resolve(checks === 1 ? json(USER) : json(AUTH_REQUIRED, 401));
    });

    function ExpiryHarness() {
      const auth = useAuth();
      return auth.notice === "expired" ? (
        <LoginForm nextPath={null} />
      ) : (
        <button type="button" onClick={auth.refresh}>
          Recheck
        </button>
      );
    }

    const user = userEvent.setup();
    render(
      <AuthProvider>
        <ExpiryHarness />
      </AuthProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "Recheck" }));

    expect(await screen.findByText(SESSION_EXPIRED_MESSAGE)).toBeDefined();
  });
});

describe("login page for an already authenticated user", () => {
  it("redirects to the dashboard instead of showing the form", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json(USER)));
    renderLogin("/attendance?limit=25");

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/attendance?limit=25");
    });
    expect(screen.queryByLabelText(/^Username/)).toBeNull();
  });

  it("ignores an unsafe next value and uses the default route", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json(USER)));
    renderLogin("https://evil.test/steal");

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/overview");
    });
    expect(replaceMock).not.toHaveBeenCalledWith(
      expect.stringContaining("evil.test"),
    );
  });
});
