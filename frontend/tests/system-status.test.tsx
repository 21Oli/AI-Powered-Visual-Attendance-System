import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SystemStatusView } from "../src/components/system-status/SystemStatusView";
import { ApiError } from "../src/lib/api/errors";
import { HEALTH_TIMEOUT_CODE, fetchHealth } from "../src/lib/api/health";

vi.mock("../src/lib/api/health", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api/health")>();
  return { ...actual, fetchHealth: vi.fn() };
});

const healthMock = vi.mocked(fetchHealth);

function apiError(
  kind: ApiError["kind"],
  code: string,
  message: string,
  status?: number,
): ApiError {
  return new ApiError({
    kind,
    code,
    message,
    ...(status === undefined ? {} : { status }),
  });
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (cause: unknown) => void = () => undefined;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  healthMock.mockReset();
  healthMock.mockResolvedValue({ status: "ok" });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("system status: states", () => {
  it("starts in a loading state with a guarded refresh", async () => {
    const pending = deferred<{ status: "ok" }>();
    healthMock.mockImplementation(() => pending.promise);
    const user = userEvent.setup();
    render(<SystemStatusView dataSourceKind="live" />);

    expect(screen.getByRole("status").textContent).toMatch(
      /waiting for the backend api to answer/i,
    );
    const button = screen.getByRole("button", { name: "Checking…" });
    expect(button.getAttribute("disabled")).not.toBeNull();

    await user.click(button);
    expect(healthMock).toHaveBeenCalledTimes(1);

    pending.resolve({ status: "ok" });
    expect(await screen.findByText(/Backend API reachable/)).toBeDefined();
  });

  it("shows healthy only after a valid response", async () => {
    render(<SystemStatusView dataSourceKind="live" />);

    const line = await screen.findByText(/Backend API reachable/);
    expect(line.closest("p")?.textContent).toMatch(/Health check at|health check at/i);
    expect(screen.getByText("Healthy")).toBeDefined();
    expect(screen.queryByText("Not yet")).toBeNull();
    await waitFor(() => {
      expect(healthMock).toHaveBeenCalledTimes(1);
    });
  });

  it("reports an unreachable backend without claiming a result", async () => {
    healthMock.mockRejectedValue(
      apiError(
        "network",
        "NETWORK_UNAVAILABLE",
        "The dashboard could not reach its API.",
      ),
    );
    render(<SystemStatusView dataSourceKind="live" />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Backend API unreachable/);
    expect(alert.textContent).toMatch(/NETWORK_UNAVAILABLE/);
    expect(alert.textContent).toMatch(/No retry was attempted automatically/);
    expect(screen.queryByText(/Backend API reachable/)).toBeNull();
    expect(screen.queryByText("Healthy")).toBeNull();
    expect(screen.getByText("Not yet")).toBeDefined();
  });

  it("distinguishes a timeout from an ordinary failure", async () => {
    healthMock.mockRejectedValue(
      apiError("network", HEALTH_TIMEOUT_CODE, "Timed out."),
    );
    render(<SystemStatusView dataSourceKind="live" />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Health check timed out/);
    expect(alert.textContent).toMatch(/8 seconds/);
    expect(alert.textContent).toMatch(/not evidence that the service is down/);
    expect(screen.queryByText(/Backend API reachable/)).toBeNull();
  });

  it("treats an unexpected payload as not healthy", async () => {
    healthMock.mockRejectedValue(
      apiError(
        "malformed",
        "MALFORMED_RESPONSE",
        "The service returned an unexpected response shape.",
      ),
    );
    render(<SystemStatusView dataSourceKind="live" />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Unexpected health response/);
    expect(alert.textContent).toMatch(/status/);
    expect(alert.textContent).toMatch(/MALFORMED_RESPONSE/);
    expect(screen.queryByText("Healthy")).toBeNull();
  });

  it("treats an unexpected HTTP status as an error", async () => {
    healthMock.mockRejectedValue(
      apiError(
        "http",
        "PROXY_METHOD_NOT_ALLOWED",
        "This route does not accept that method.",
        405,
      ),
    );
    render(<SystemStatusView dataSourceKind="live" />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Unexpected health response/);
    expect(alert.textContent).toMatch(/HTTP 405/);
    expect(screen.queryByText("Healthy")).toBeNull();
  });

  it("reports a server error with only a sanitized reference", async () => {
    healthMock.mockRejectedValue(
      apiError(
        "http",
        "BACKEND_UNAVAILABLE",
        "The dashboard backend could not be reached.",
        503,
      ),
    );
    render(<SystemStatusView dataSourceKind="live" />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Backend API returned a server error/);
    expect(alert.textContent).toMatch(/BACKEND_UNAVAILABLE/);
    expect(alert.textContent).toMatch(/HTTP 503/);
    expect(alert.textContent).not.toMatch(/http:\/\/127|BACKEND_BASE_URL|stack/i);
  });
});

describe("system status: refresh", () => {
  it("re-checks on demand and replaces the previous result", async () => {
    const user = userEvent.setup();
    render(<SystemStatusView dataSourceKind="live" />);
    await screen.findByText(/Backend API reachable/);

    healthMock.mockRejectedValueOnce(
      apiError("network", "NETWORK_UNAVAILABLE", "Unreachable."),
    );
    await user.click(screen.getByRole("button", { name: "Refresh" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Backend API unreachable/);
    expect(screen.queryByText(/Backend API reachable/)).toBeNull();
    expect(healthMock).toHaveBeenCalledTimes(2);
  });

  it("does not start a second request while one is running", async () => {
    const pending = deferred<{ status: "ok" }>();
    healthMock.mockImplementation(() => pending.promise);
    const user = userEvent.setup();
    render(<SystemStatusView dataSourceKind="live" />);

    const button = screen.getByRole("button", { name: "Checking…" });
    await user.click(button);
    await user.click(button);

    expect(healthMock).toHaveBeenCalledTimes(1);

    pending.resolve({ status: "ok" });
    expect(await screen.findByText(/Backend API reachable/)).toBeDefined();
  });

  it("aborts an in-flight check when the view unmounts", async () => {
    const pending = deferred<{ status: "ok" }>();
    healthMock.mockImplementation(() => pending.promise);
    const view = render(<SystemStatusView dataSourceKind="live" />);
    await waitFor(() => {
      expect(healthMock).toHaveBeenCalledTimes(1);
    });

    view.unmount();

    const signal = healthMock.mock.calls[0]?.[0];
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal?.aborted).toBe(true);

    pending.resolve({ status: "ok" });
    expect(document.body.textContent).not.toMatch(/Backend API reachable/);
  });
});

describe("system status: fixture mode", () => {
  it("discloses fixture mode and never fabricates a healthy backend", async () => {
    render(<SystemStatusView dataSourceKind="fixture" />);

    const note = screen.getByRole("note");
    expect(note.textContent).toContain("FIXTURE DATA");
    expect(note.textContent).toMatch(/live backend health has not been checked/i);
    expect(healthMock).not.toHaveBeenCalled();
    expect(screen.queryByText("Healthy")).toBeNull();
    expect(screen.queryByText(/Backend API reachable/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("Not checked")).toBeDefined();
    expect(
      screen.getByRole("button", { name: "Refresh" }).getAttribute("disabled"),
    ).not.toBeNull();
  });
});

describe("system status: scope and accessibility", () => {
  it("states what the health check does not verify", () => {
    render(<SystemStatusView dataSourceKind="live" />);

    expect(
      screen.getByRole("heading", { level: 1, name: "System Status" }),
    ).toBeDefined();
    expect(
      screen.getAllByRole("heading", { level: 1 }),
    ).toHaveLength(1);
    expect(
      screen.getByRole("region", { name: "Backend API" }),
    ).toBeDefined();
    expect(
      screen.getByRole("region", { name: "Scope and limitations" }),
    ).toBeDefined();

    const scope = screen.getByRole("region", { name: "Scope and limitations" });
    for (const item of [
      /PostgreSQL availability/,
      /Camera availability/,
      /Face detection or recognition accuracy/,
      /Biometric enrolment/,
      /Authentication or authorization/,
      /Production deployment readiness/,
    ]) {
      expect(scope.textContent).toMatch(item);
    }
  });
});
