import { StrictMode, useState } from "react";
import type { ReactNode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "../src/lib/auth/AuthProvider";

/**
 * Session-state tests.
 *
 * Everything here is mocked at the `fetch` boundary: no backend, no database
 * and no cookie jar. The provider under test is the real one, so the
 * distinction between "401 -> unauthenticated" and "network/5xx -> error" is
 * exercised as written.
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

const UNAVAILABLE = {
  error: {
    code: "DATABASE_UNAVAILABLE",
    message: "Database is temporarily unavailable.",
  },
};

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function routeFetch(options: {
  me?: () => Promise<Response>;
  login?: () => Promise<Response>;
  logout?: () => Promise<Response>;
}) {
  fetchMock.mockImplementation((input) => {
    const url = String(input);
    if (url.endsWith("/api/v1/auth/me")) {
      return (options.me ?? (() => Promise.resolve(json(USER))))();
    }
    if (url.endsWith("/api/v1/auth/login")) {
      return (options.login ?? (() => Promise.resolve(json({ user: USER }))))();
    }
    if (url.endsWith("/api/v1/auth/logout")) {
      return (
        options.logout ?? (() => Promise.resolve(new Response(null, { status: 204 })))
      )();
    }
    return Promise.resolve(json({ error: { code: "NONE", message: "none" } }, 404));
  });
}

function StatusProbe() {
  const auth = useAuth();
  return (
    <p data-testid="status">
      {`${auth.status}:${auth.user?.username ?? "-"}:${auth.notice ?? "-"}`}
    </p>
  );
}

function LogoutProbe() {
  const auth = useAuth();
  const [failed, setFailed] = useState(false);
  return (
    <div>
      <p data-testid="status">{auth.status}</p>
      <p data-testid="failed">{failed ? "yes" : "no"}</p>
      <button
        type="button"
        onClick={() => {
          void auth.signOut().catch(() => setFailed(true));
        }}
      >
        Sign out
      </button>
    </div>
  );
}

function renderProvider(children: ReactNode) {
  return render(<AuthProvider>{children}</AuthProvider>);
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("session establishment", () => {
  it("retrieves the current user and exposes the authenticated state", async () => {
    routeFetch({});
    renderProvider(<StatusProbe />);

    expect(await screen.findByText("authenticated:ada:-")).toBeDefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/v1/auth/me");
  });

  it("restores the session after a refresh (remount) without asking the user", async () => {
    routeFetch({});
    const first = renderProvider(<StatusProbe />);
    await screen.findByText("authenticated:ada:-");
    first.unmount();

    renderProvider(<StatusProbe />);

    expect(await screen.findByText("authenticated:ada:-")).toBeDefined();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("issues one current-user request under StrictMode double effects", async () => {
    routeFetch({});
    render(
      <StrictMode>
        <AuthProvider>
          <StatusProbe />
        </AuthProvider>
      </StrictMode>,
    );

    expect(await screen.findByText("authenticated:ada:-")).toBeDefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("ignores a retry while a session check is already in flight", async () => {
    let releaseMe: ((response: Response) => void) | undefined;
    routeFetch({
      me: () =>
        new Promise<Response>((resolve) => {
          releaseMe = resolve;
        }),
    });

    function RetryProbe() {
      const auth = useAuth();
      return (
        <div>
          <p data-testid="status">{auth.status}</p>
          <button type="button" onClick={auth.refresh}>
            Retry
          </button>
        </div>
      );
    }

    const user = userEvent.setup();
    renderProvider(<RetryProbe />);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    releaseMe?.(json(USER));
    expect(await screen.findByText("authenticated")).toBeDefined();
  });
});

describe("expired and revoked sessions", () => {
  it("treats a cold 401 as unauthenticated without claiming an expiry", async () => {
    routeFetch({ me: () => Promise.resolve(json(AUTH_REQUIRED, 401)) });
    renderProvider(<StatusProbe />);

    // No session existed in this tab, so nothing expired.
    expect(await screen.findByText("unauthenticated:-:-")).toBeDefined();
  });

  it("flags an expiry only after this tab held an authenticated session", async () => {
    let checks = 0;
    routeFetch({
      me: () => {
        checks += 1;
        return Promise.resolve(checks === 1 ? json(USER) : json(AUTH_REQUIRED, 401));
      },
    });

    function RecheckProbe() {
      const auth = useAuth();
      return (
        <div>
          <p data-testid="status">
            {`${auth.status}:${auth.user?.username ?? "-"}:${auth.notice ?? "-"}`}
          </p>
          <button type="button" onClick={auth.refresh}>
            Recheck
          </button>
        </div>
      );
    }

    const user = userEvent.setup();
    renderProvider(<RecheckProbe />);
    expect(await screen.findByText("authenticated:ada:-")).toBeDefined();

    await user.click(screen.getByRole("button", { name: "Recheck" }));

    expect(await screen.findByText("unauthenticated:-:expired")).toBeDefined();
  });

  it("treats a network failure as an error, never as signed out", async () => {
    routeFetch({ me: () => Promise.reject(new TypeError("fetch failed")) });
    renderProvider(<StatusProbe />);

    expect(await screen.findByText("error:-:-")).toBeDefined();
  });

  it("treats a 503 as an error, never as signed out", async () => {
    routeFetch({ me: () => Promise.resolve(json(UNAVAILABLE, 503)) });
    renderProvider(<StatusProbe />);

    expect(await screen.findByText("error:-:-")).toBeDefined();
  });

  it("treats a malformed 200 body as an error, never as signed out", async () => {
    routeFetch({
      me: () => Promise.resolve(json({ user_id: "only-a-fragment" })),
    });
    renderProvider(<StatusProbe />);

    expect(await screen.findByText("error:-:-")).toBeDefined();
  });
});

describe("logout", () => {
  it("clears the client state only after the backend confirms revocation", async () => {
    routeFetch({});
    const user = userEvent.setup();
    renderProvider(<LogoutProbe />);

    await screen.findByText("authenticated");
    await user.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByText("unauthenticated")).toBeDefined();
    const logoutCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith("/api/v1/auth/logout"),
    );
    expect(logoutCall).toBeDefined();
    expect((logoutCall?.[1] as RequestInit).method).toBe("POST");
    expect(screen.getByTestId("failed").textContent).toBe("no");
  });

  it("keeps the session and reports failure when sign-out does not succeed", async () => {
    routeFetch({
      logout: () =>
        Promise.resolve(
          json(
            { error: { code: "BACKEND_UNAVAILABLE", message: "Unreachable." } },
            502,
          ),
        ),
    });
    const user = userEvent.setup();
    renderProvider(<LogoutProbe />);

    await screen.findByText("authenticated");
    await user.click(screen.getByRole("button", { name: "Sign out" }));

    expect(await screen.findByText("yes")).toBeDefined();
    expect(screen.getByTestId("status").textContent).toBe("authenticated");
  });

  it("keeps the session when the backend cannot be reached at all", async () => {
    routeFetch({ logout: () => Promise.reject(new TypeError("offline")) });
    const user = userEvent.setup();
    renderProvider(<LogoutProbe />);

    await screen.findByText("authenticated");
    await user.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => {
      expect(screen.getByTestId("failed").textContent).toBe("yes");
    });
    expect(screen.getByTestId("status").textContent).toBe("authenticated");
  });
});
