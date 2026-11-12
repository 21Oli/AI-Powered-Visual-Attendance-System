import { API_PATHS, requestJson } from "./client";
import { ApiError } from "./errors";
import { parseHealth } from "./types";
import type { HealthResponse } from "./types";

/**
 * Typed client for the one health route the backend publishes.
 *
 * The endpoint is a **liveness** probe: the FastAPI process answers
 * `{ "status": "ok" }` without touching PostgreSQL, cameras or anything else.
 * The parser therefore accepts exactly that payload and nothing else, and every
 * failure is surfaced as an `ApiError` rather than being turned into a healthy
 * result.
 */

/** Wall-clock budget for one health attempt (documented in the UI). */
export const HEALTH_TIMEOUT_MS = 8_000;

/** Distinguishes a timeout from an ordinary unreachable backend. */
export const HEALTH_TIMEOUT_CODE = "HEALTH_TIMEOUT";

export const HEALTH_TIMEOUT_MESSAGE =
  "The backend health check did not complete within its timeout.";

export interface HealthRequestOptions {
  /**
   * Override the documented 8-second budget. Only tests use this; the page
   * always calls `fetchHealth(signal)` with the default.
   */
  readonly timeoutMs?: number;
}

/**
 * Ask the backend API whether it is running.
 *
 * Uses the same same-origin transport as every other request and aborts after
 * `timeoutMs`, so a hung backend cannot leave the screen "checking" forever.
 * Caller cancellation (component unmount) is **not** reported as a timeout: the
 * original rejection is rethrown untouched, so an intentional abort can never be
 * mistaken for a slow backend.
 */
export async function fetchHealth(
  signal?: AbortSignal,
  options: HealthRequestOptions = {},
): Promise<HealthResponse> {
  const timeout = AbortSignal.timeout(options.timeoutMs ?? HEALTH_TIMEOUT_MS);
  const combined = signal === undefined ? timeout : AbortSignal.any([signal, timeout]);

  try {
    return await requestJson({
      path: API_PATHS.health,
      signal: combined,
      parse: parseHealth,
    });
  } catch (error) {
    if (timeout.aborted && signal?.aborted !== true) {
      throw new ApiError({
        kind: "network",
        code: HEALTH_TIMEOUT_CODE,
        message: HEALTH_TIMEOUT_MESSAGE,
      });
    }
    throw error;
  }
}
