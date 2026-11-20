import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { replaceMock } = vi.hoisted(() => ({ replaceMock: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: replaceMock,
    push: vi.fn(),
    refresh: vi.fn(),
  }),
  usePathname: () => "/employees",
}));

import { RequireAuth } from "../src/components/auth/RequireAuth";
import { EmployeesView } from "../src/components/employees/EmployeesView";
import { AuthProvider } from "../src/lib/auth/AuthProvider";
import { ACCESS_DENIED_MESSAGE } from "../src/lib/api/errors";

/**
 * How the dashboard distinguishes the three failures that look similar to a
 * user: the session ended (401), the role is not allowed (403), and the service
 * is unavailable (503/network). Only the first ends the session UI, only the
 * second is an authorization outcome, and none of them is retried automatically.
 */

const USER = {
  user_id: "user-1",
  username: "ada",
  role: "Admin",
  is_active: true,
};

const employee = {
  employee_id: "EMP-1",
  full_name: "Ada Example",
  is_active: true,
  created_at: "2026-01-01T00:00:00+00:00",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const fetchMock = vi.fn<typeof fetch>();

interface Handlers {
  readonly me?: () => Response;
  readonly list?: () => Response;
  readonly create?: () => Response;
}

function routeApi(handlers: Handlers = {}) {
  fetchMock.mockImplementation((input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.endsWith("/api/v1/auth/me")) {
      return Promise.resolve(handlers.me?.() ?? json(USER));
    }
    if (method === "POST") {
      return Promise.resolve(
        handlers.create?.() ?? json(employee, 201),
      );
    }
    return Promise.resolve(
      handlers.list?.() ??
        json({ employees: [employee], count: 1, limit: 20, next_after: null }),
    );
  });
}

function renderGuarded() {
  return render(
    <AuthProvider>
      <RequireAuth>
        <EmployeesView />
      </RequireAuth>
    </AuthProvider>,
  );
}

beforeEach(() => {
  replaceMock.mockReset();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  window.history.replaceState({}, "", "/employees");
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("401 — the session ended", () => {
  it("clears the signed-in UI and redirects with a safe return path", async () => {
    routeApi({
      list: () =>
        json(
          {
            error: {
              code: "AUTHENTICATION_REQUIRED",
              message: "Authentication is required.",
            },
          },
          401,
        ),
    });

    renderGuarded();
    await screen.findByRole("button", { name: /create employee/i });

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/login?next=%2Femployees");
    });
    // The directory is unmounted, so no stale data stays on screen.
    expect(screen.queryByRole("heading", { name: "Employees" })).toBeNull();
    expect(replaceMock).toHaveBeenCalledTimes(1);
  });

  it("does not change the session when /me itself fails with 401", async () => {
    routeApi({
      me: () =>
        json(
          {
            error: {
              code: "AUTHENTICATION_REQUIRED",
              message: "Authentication is required.",
            },
          },
          401,
        ),
    });

    renderGuarded();

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/login?next=%2Femployees");
    });
  });
});

describe("403 — authenticated but forbidden", () => {
  it("keeps the session, reports the denial, and does not retry", async () => {
    routeApi({
      create: () =>
        json(
          {
            error: {
              code: "PERMISSION_DENIED",
              message: "You do not have permission to perform this action.",
            },
          },
          403,
        ),
    });

    const user = userEvent.setup();
    renderGuarded();

    await screen.findByText("EMP-1");
    await user.click(screen.getByRole("button", { name: /create employee/i }));
    await user.type(screen.getByLabelText(/employee id/i), "EMP-2");
    await user.type(screen.getByLabelText(/full name/i), "Grace Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    expect(await screen.findByText(ACCESS_DENIED_MESSAGE)).toBeDefined();

    // The session is intact: no redirect, the page is still there.
    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Employees" })).toBeDefined();

    // One attempt only — a forbidden write is never replayed.
    const posts = () =>
      fetchMock.mock.calls.filter(
        (call) => (call[1]?.method ?? "GET").toUpperCase() === "POST",
      ).length;
    expect(posts()).toBe(1);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(posts()).toBe(1);
  });

  it("shows an access-denied panel for a forbidden read with no retry", async () => {
    routeApi({
      list: () =>
        json(
          {
            error: {
              code: "PERMISSION_DENIED",
              message: "You do not have permission to perform this action.",
            },
          },
          403,
        ),
    });

    renderGuarded();

    expect(await screen.findByText("Access denied")).toBeDefined();
    expect(
      screen.getByText("You do not have permission to perform this action."),
    ).toBeDefined();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(replaceMock).not.toHaveBeenCalled();
  });
});

describe("service failures", () => {
  it("does not treat a 503 as an ended session", async () => {
    routeApi({
      list: () =>
        json(
          {
            error: {
              code: "DATABASE_UNAVAILABLE",
              message: "Database is temporarily unavailable.",
            },
          },
          503,
        ),
    });

    renderGuarded();

    expect(
      await screen.findByText("The employee directory is unavailable"),
    ).toBeDefined();
    expect(screen.getByRole("button", { name: "Retry" })).toBeDefined();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("does not treat a network failure as an ended session", async () => {
    fetchMock.mockImplementation((input) => {
      if (String(input).endsWith("/api/v1/auth/me")) {
        return Promise.resolve(json(USER));
      }
      return Promise.reject(new TypeError("Failed to fetch"));
    });

    renderGuarded();

    await waitFor(() => {
      expect(
        screen.getByText("The dashboard could not reach its API."),
      ).toBeDefined();
    });
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("keeps an uncertain create outcome unclaimed and unretried", async () => {
    routeApi({
      create: () =>
        json(
          {
            error: {
              code: "AUDIT_RESULT_UNCERTAIN",
              message: "The operation may have been applied.",
            },
          },
          503,
        ),
    });

    const user = userEvent.setup();
    renderGuarded();

    await screen.findByText("EMP-1");
    await user.click(screen.getByRole("button", { name: /create employee/i }));
    await user.type(screen.getByLabelText(/employee id/i), "EMP-2");
    await user.type(screen.getByLabelText(/full name/i), "Grace Example");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/temporarily unavailable|unable to/i);

    const posts = () =>
      fetchMock.mock.calls.filter(
        (call) => (call[1]?.method ?? "GET").toUpperCase() === "POST",
      ).length;
    expect(posts()).toBe(1);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(posts()).toBe(1);
  });
});
