import { ApiError, malformedError, networkError } from "./errors";
import { parseErrorEnvelope } from "./types";

/**
 * Same-origin HTTP transport for the dashboard.
 *
 * Every request goes to `/api/backend/...`, never to the FastAPI service
 * directly: the browser cannot learn, reach or override the backend origin.
 * Responses are validated by a caller-supplied parser, so a 2xx status is never
 * treated as proof that the body is the contracted shape.
 */

export const PROXY_PREFIX = "/api/backend";

/** The eight approved routes, expressed once. */
export const API_PATHS = {
  health: "/api/v1/health",
  employees: "/api/v1/employees",
  attendance: "/api/v1/attendance",
  employee: (employeeId: string) =>
    `/api/v1/employees/${encodeURIComponent(employeeId)}`,
  employeeDeactivate: (employeeId: string) =>
    `/api/v1/employees/${encodeURIComponent(employeeId)}/deactivate`,
  checkout: (employeeId: string) =>
    `/api/v1/attendance/${encodeURIComponent(employeeId)}/checkout`,
} as const;

export type HttpMethod = "GET" | "POST" | "PATCH";

/** Build the same-origin URL for a backend path (single construction point). */
export function buildProxyUrl(path: string, search?: URLSearchParams): string {
  const query = search?.toString() ?? "";
  return `${PROXY_PREFIX}${path}${query.length > 0 ? `?${query}` : ""}`;
}

export interface RequestOptions<T> {
  readonly path: string;
  readonly method?: HttpMethod;
  readonly search?: URLSearchParams;
  /** JSON body; omit entirely for routes whose contract has no body. */
  readonly body?: unknown;
  readonly signal?: AbortSignal;
  readonly parse: (raw: unknown) => T | null;
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

export async function requestJson<T>(options: RequestOptions<T>): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { accept: "application/json" };
  let body: string | undefined;

  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(buildProxyUrl(options.path, options.search), {
      method,
      headers,
      body,
      signal: options.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (cause) {
    if (options.signal?.aborted) {
      // Cancellation is control flow, not a user-facing failure.
      throw cause;
    }
    throw networkError();
  }

  const text = await response.text();

  if (!response.ok) {
    const envelope = parseErrorEnvelope(parseJson(text));
    if (envelope !== null) {
      throw new ApiError({
        kind: "http",
        status: response.status,
        code: envelope.error.code,
        message: envelope.error.message,
        reason: envelope.error.reason,
        fields: envelope.fields,
      });
    }
    throw new ApiError({
      kind: "http",
      status: response.status,
      code: "HTTP_ERROR",
      message: `The service responded with status ${response.status}.`,
    });
  }

  const parsedBody = parseJson(text);
  if (parsedBody === undefined) {
    throw malformedError("The service returned a response that is not valid JSON.");
  }

  const value = options.parse(parsedBody);
  if (value === null) {
    throw malformedError("The service returned an unexpected response shape.");
  }
  return value;
}
