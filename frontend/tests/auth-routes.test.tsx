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

import { AuthProvider } from "../src/lib/auth/AuthProvider";
import { RequireAuth } from "../src/components/auth/RequireAuth";

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

function renderGuarded() {
  return render(
    <AuthProvider>
      <RequireAuth>
        <h1>Protected overview</h1>
      </RequireAuth>
    </AuthProvider>,
  );
}

beforeEach(() => {
  replaceMock.mockReset();
  fetchMock.mockReset();
  fetchMock.mockImplementation(() => Promise.resolve(json(USER)));
  vi.stubGlobal("fetch", fetchMock);
  window.history.replaceState({}, "", "/employees");
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("protected dashboard routes", () => {
  it("shows no protected content while the session is being established", () => {
    fetchMock.mockImplementation(() => new Promise<Response>(() => undefined));
    renderGuarded();

    expect(screen.queryByText("Protected overview")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain(
      "Checking your session",
    );
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("redirects an unauthenticated visitor to sign-in with a safe return path", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json(AUTH_REQUIRED, 401)));
    renderGuarded();

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/login?next=%2Femployees");
    });
    expect(screen.queryByText("Protected overview")).toBeNull();
  });

  it("renders the dashboard for an authenticated visitor", async () => {
    renderGuarded();

    expect(
      await screen.findByRole("heading", { name: "Protected overview" }),
    ).toBeDefined();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("does not redirect when the backend is unavailable, and offers a retry", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        json(
          {
            error: {
              code: "DATABASE_UNAVAILABLE",
              message: "Database is temporarily unavailable.",
            },
          },
          503,
        ),
      ),
    );
    const user = userEvent.setup();
    renderGuarded();

    expect(await screen.findByText("Session check failed")).toBeDefined();
    expect(replaceMock).not.toHaveBeenCalled();
    expect(screen.queryByText("Protected overview")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("cannot loop: a signed-in visitor is never sent to the login page", async () => {
    renderGuarded();
    await screen.findByRole("heading", { name: "Protected overview" });

    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("does not repeat the session check on every render", async () => {
    const { rerender } = renderGuarded();
    await screen.findByRole("heading", { name: "Protected overview" });

    rerender(
      <AuthProvider>
        <RequireAuth>
          <h1>Protected overview</h1>
        </RequireAuth>
      </AuthProvider>,
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
