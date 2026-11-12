import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { checkoutEmployee } from "../src/lib/api/attendance";
import { isApiError } from "../src/lib/api/errors";

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const checkoutEvent = {
  event_id: "EV-1",
  employee_id: "EMP-1",
  event_type: "check_out",
  occurred_at: "2026-11-11T18:00:00+00:00",
};

function firstInit(): RequestInit | undefined {
  return fetchMock.mock.calls[0]?.[1];
}

async function caught(promise: Promise<unknown>): Promise<unknown> {
  return promise.catch((cause: unknown) => cause);
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("checkout request construction", () => {
  it("posts to the checkout route through the same-origin proxy", async () => {
    fetchMock.mockResolvedValueOnce(json(checkoutEvent, 201));

    await checkoutEmployee("EMP-1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/backend/api/v1/attendance/EMP-1/checkout",
    );
    expect(firstInit()?.method).toBe("POST");
    expect(firstInit()?.credentials).toBe("same-origin");
    expect(firstInit()?.cache).toBe("no-store");
  });

  it("sends no body and therefore no content-type", async () => {
    fetchMock.mockResolvedValueOnce(json(checkoutEvent, 201));

    await checkoutEmployee("EMP-1");

    expect(firstInit()?.body).toBeUndefined();
    expect(firstInit()?.headers).toEqual({ accept: "application/json" });
  });

  it("encodes the employee id as a single path segment", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ ...checkoutEvent, employee_id: "EMP 1" }, 201),
    );

    await checkoutEmployee("EMP 1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/backend/api/v1/attendance/EMP%201/checkout",
    );
  });

  it("forwards the abort signal", async () => {
    const controller = new AbortController();
    fetchMock.mockResolvedValueOnce(json(checkoutEvent, 201));

    await checkoutEmployee("EMP-1", controller.signal);

    expect(firstInit()?.signal).toBe(controller.signal);
  });

  it("never targets the backend origin directly", async () => {
    fetchMock.mockResolvedValueOnce(json(checkoutEvent, 201));

    await checkoutEmployee("EMP-1");

    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url.startsWith("/api/backend/")).toBe(true);
    expect(url.includes("://")).toBe(false);
  });
});

describe("checkout success", () => {
  it("returns the validated check-out event", async () => {
    fetchMock.mockResolvedValueOnce(json(checkoutEvent, 201));

    const event = await checkoutEmployee("EMP-1");

    expect(event).toEqual(checkoutEvent);
    expect(event.event_type).toBe("check_out");
    expect(event.employee_id).toBe("EMP-1");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("rejects a success body that is not an event", async () => {
    fetchMock.mockResolvedValueOnce(json({ ok: true }, 201));

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "malformed",
      code: "MALFORMED_RESPONSE",
    });
  });

  it("rejects a check-in returned as a check-out result", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ ...checkoutEvent, event_type: "check_in" }, 201),
    );

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "malformed",
    });
  });

  it("rejects an event belonging to a different employee", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ ...checkoutEvent, employee_id: "EMP-2" }, 201),
    );

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "malformed",
    });
  });

  it("rejects a payload missing a required field", async () => {
    const { occurred_at: _omitted, ...withoutTimestamp } = checkoutEvent;
    void _omitted;
    fetchMock.mockResolvedValueOnce(json(withoutTimestamp, 201));

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "malformed",
    });
  });

  it("rejects an empty timestamp the shared parser refuses", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ ...checkoutEvent, occurred_at: "" }, 201),
    );

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "malformed",
    });
  });

  it("rejects a non-JSON success body", async () => {
    fetchMock.mockResolvedValueOnce(new Response("<html>ok</html>", { status: 201 }));

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "malformed",
    });
  });
});

describe("checkout error propagation", () => {
  it("propagates an unknown employee as a 404 business error", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "EMPLOYEE_NOT_FOUND",
            message: "No employee exists for the requested identifier.",
            reason: "EMPLOYEE_NOT_FOUND",
          },
        },
        404,
      ),
    );

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "http",
      status: 404,
      code: "EMPLOYEE_NOT_FOUND",
      reason: "EMPLOYEE_NOT_FOUND",
    });
  });

  it.each(["EMPLOYEE_INACTIVE", "NO_OPEN_SESSION", "CHECKOUT_COOLDOWN"])(
    "preserves the %s reason on a 409 refusal",
    async (reason) => {
      fetchMock.mockResolvedValueOnce(
        json(
          {
            error: {
              code: "CHECKOUT_REJECTED",
              message: "The check-out was refused.",
              reason,
            },
          },
          409,
        ),
      );

      await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
        status: 409,
        code: "CHECKOUT_REJECTED",
        reason,
      });
    },
  );

  it("preserves a 409 refusal with no reason rather than inventing one", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        { error: { code: "CHECKOUT_REJECTED", message: "Refused." } },
        409,
      ),
    );

    const error = await caught(checkoutEmployee("EMP-1"));

    expect(isApiError(error)).toBe(true);
    if (isApiError(error)) {
      expect(error.code).toBe("CHECKOUT_REJECTED");
      expect(error.reason).toBeUndefined();
    }
  });

  it("distinguishes a confirmed persistence rejection", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "PERSISTENCE_REJECTED",
            message:
              "The attendance event was rejected before any write; the journal is unchanged.",
          },
        },
        422,
      ),
    );

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      status: 422,
      code: "PERSISTENCE_REJECTED",
    });
  });

  it("keeps an unconfirmed persistence outcome as an error and does not retry", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "PERSISTENCE_UNCONFIRMED",
            message:
              "The check-out could not be confirmed as persisted; it was not retried.",
          },
        },
        503,
      ),
    );

    const error = await caught(checkoutEmployee("EMP-1"));

    expect(isApiError(error)).toBe(true);
    if (isApiError(error)) {
      expect(error.status).toBe(503);
      expect(error.code).toBe("PERSISTENCE_UNCONFIRMED");
    }
    // No optimistic result, no automatic retry.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["ATTENDANCE_UNAVAILABLE", 503],
    ["DATABASE_UNAVAILABLE", 503],
    ["INTERNAL_ERROR", 500],
  ])("propagates %s as an HTTP failure", async (code, status) => {
    fetchMock.mockResolvedValueOnce(
      json({ error: { code, message: "Sanitized failure." } }, status),
    );

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      status,
      code,
    });
  });

  it.each([
    ["BACKEND_UNAVAILABLE", 502],
    ["BACKEND_TIMEOUT", 504],
  ])("propagates the proxy failure %s", async (code, status) => {
    fetchMock.mockResolvedValueOnce(
      json({ error: { code, message: "The service could not be reached." } }, status),
    );

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "http",
      status,
      code,
    });
  });

  it("reports a network failure as a network error", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "network",
      code: "NETWORK_UNAVAILABLE",
    });
  });

  it("preserves an unknown error code without reinterpreting it", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ error: { code: "SOMETHING_NEW", message: "Unknown." } }, 409),
    );

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      code: "SOMETHING_NEW",
    });
  });

  it("falls back to a generic code for a non-JSON error body", async () => {
    fetchMock.mockResolvedValueOnce(new Response("gateway down", { status: 502 }));

    await expect(checkoutEmployee("EMP-1")).rejects.toMatchObject({
      kind: "http",
      status: 502,
      code: "HTTP_ERROR",
    });
  });

  it("does not report an aborted request as an API error", async () => {
    const controller = new AbortController();
    controller.abort();
    const abortError = new Error("aborted");
    abortError.name = "AbortError";
    fetchMock.mockRejectedValueOnce(abortError);

    const error = await caught(checkoutEmployee("EMP-1", controller.signal));

    expect(isApiError(error)).toBe(false);
    expect((error as Error).name).toBe("AbortError");
  });
});
