import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../src/lib/api/errors";
import { HEALTH_TIMEOUT_CODE, fetchHealth } from "../src/lib/api/health";

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("health API client", () => {
  it("requests the health route through the same-origin proxy", async () => {
    fetchMock.mockResolvedValue(json({ status: "ok" }));

    const result = await fetchHealth();

    expect(result).toEqual({ status: "ok" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [input, init] = fetchMock.mock.calls[0] ?? [];
    expect(String(input)).toBe("/api/backend/api/v1/health");
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.body).toBeUndefined();
    expect(new Headers(init?.headers).get("content-type")).toBeNull();
  });

  it("accepts only the documented health payload", async () => {
    fetchMock.mockResolvedValue(json({ status: "degraded" }));

    const error = await fetchHealth().catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).kind).toBe("malformed");
    expect((error as ApiError).code).toBe("MALFORMED_RESPONSE");
  });

  it("rejects a body that is not valid JSON", async () => {
    fetchMock.mockResolvedValue(
      new Response("not json", {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const error = await fetchHealth().catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).kind).toBe("malformed");
  });

  it("surfaces an HTTP error with its sanitized code", async () => {
    fetchMock.mockResolvedValue(
      json(
        {
          error: {
            code: "BACKEND_UNAVAILABLE",
            message: "The dashboard backend could not be reached.",
          },
        },
        503,
      ),
    );

    const error = (await fetchHealth().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.kind).toBe("http");
    expect(error.status).toBe(503);
    expect(error.code).toBe("BACKEND_UNAVAILABLE");
  });

  it("reports an unreachable backend as a network failure", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const error = (await fetchHealth().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.kind).toBe("network");
    expect(error.code).toBe("NETWORK_UNAVAILABLE");
  });

  it("classifies a hung request as a timeout", async () => {
    fetchMock.mockImplementation(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        }),
    );

    const error = (await fetchHealth(undefined, { timeoutMs: 10 }).catch(
      (cause: unknown) => cause,
    )) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.kind).toBe("network");
    expect(error.code).toBe(HEALTH_TIMEOUT_CODE);
    expect(error.message).toMatch(/timeout/i);
  });

  it("does not report a caller cancellation as a timeout", async () => {
    const controller = new AbortController();
    fetchMock.mockImplementation(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        }),
    );

    const pending = fetchHealth(controller.signal).catch((cause: unknown) => cause);
    controller.abort();
    const error = await pending;

    expect((error as ApiError).code).not.toBe(HEALTH_TIMEOUT_CODE);
    expect((error as ApiError).message).not.toMatch(/timeout/i);
  });
});
