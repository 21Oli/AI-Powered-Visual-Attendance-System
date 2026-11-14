import { ApiError, malformedError, networkError } from "./errors";
import { parseErrorEnvelope } from "./types";

/**
 * Same-origin HTTP transport for the dashboard.
 *
 * The browser never talks to FastAPI: dashboard data goes through
 * `/api/backend/...` and the authentication boundary through
 * `/api/v1/auth/...`, both served by this Next.js application. The backend
 * origin is server-only (`BACKEND_BASE_URL`), so it cannot be learned or
 * overridden from the client.
 *
 * Responses are validated by a caller-supplied parser, so a 2xx status is
 * never treated as proof that the body is the contracted shape, and the
 * session cookie stays a browser-managed HTTP-only cookie — it is never read,
 * copied or stored by this module.
 */

export const PROXY_PREFIX = "/api/backend";

/** The approved routes, expressed once. */
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
  // Authentication (P5.4.8): same-origin BFF paths, never the backend origin.
  login: "/api/v1/auth/login",
  me: "/api/v1/auth/me",
  logout: "/api/v1/auth/logout",
} as const;

export type HttpMethod = "GET" | "POST" | "PATCH";

/** Build the same-origin URL for a backend path (single construction point). */
export function buildProxyUrl(path: string, search?: URLSearchParams): string {
  const query = search?.toString() ?? "";
  return `${PROXY_PREFIX}${path}${query.length > 0 ? `?${query}` : ""}`;
}

/** A same-origin URL for a route already rooted at `/api/...` (auth paths). */
export function buildSameOriginUrl(
  path: string,
  search?: URLSearchParams,
): string {
  const query = search?.toString() ?? "";
  return `${path}${query.length > 0 ? `?${query}` : ""}`;
}

interface SendOptions {
  readonly url: string;
  readonly method?: HttpMethod;
  readonly body?: unknown;
  readonly signal?: AbortSignal;
}

export interface RequestOptions<T> extends Omit<SendOptions, "url"> {
  readonly path: string;
  readonly search?: URLSearchParams;
  /**
   * `true` addresses the path as-is (the `/api/v1/auth/...` BFF routes), `false`
   * (the default) routes it through the `/api/backend` dashboard proxy. Both
   * are same-origin; only the auth boundary must live under `/api/v1` because
   * that is the session cookie's `Path`.
   */
  readonly direct?: boolean;
  readonly parse: (raw: unknown) => T | null;
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

async function send(options: SendOptions): Promise<Response> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { accept: "application/json" };
  let body: string | undefined;

  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  try {
    return await fetch(options.url, {
      method,
      headers,
      body,
      signal: options.signal,
      // The session cookie is an HTTP-only browser cookie: sending it is the
      // browser's job, and it is never captured in JavaScript state.
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
}

function httpErrorFrom(status: number, text: string): ApiError {
  const envelope = parseErrorEnvelope(parseJson(text));
  if (envelope !== null) {
    return new ApiError({
      kind: "http",
      status,
      code: envelope.error.code,
      message: envelope.error.message,
      reason: envelope.error.reason,
      fields: envelope.fields,
    });
  }
  return new ApiError({
    kind: "http",
    status,
    code: "HTTP_ERROR",
    message: `The service responded with status ${status}.`,
  });
}

export async function requestJson<T>(options: RequestOptions<T>): Promise<T> {
  const response = await send({
    url:
      options.direct === true
        ? buildSameOriginUrl(options.path, options.search)
        : buildProxyUrl(options.path, options.search),
    method: options.method,
    body: options.body,
    signal: options.signal,
  });

  const text = await response.text();

  if (!response.ok) {
    throw httpErrorFrom(response.status, text);
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

export interface VoidRequestOptions extends Omit<SendOptions, "url"> {
  readonly path: string;
  readonly search?: URLSearchParams;
}

/**
 * Same-origin request whose success contract is "no content" (logout: 204).
 *
 * The body is deliberately not parsed — a 204 has none, and requiring JSON
 * would turn a successful sign-out into a malformed-response error. Error
 * statuses keep the shared envelope mapping, so a failed sign-out is still a
 * typed `ApiError` rather than a silent success.
 */
export async function requestVoid(
  options: VoidRequestOptions,
): Promise<void> {
  const response = await send({
    url: buildSameOriginUrl(options.path, options.search),
    method: options.method,
    body: options.body,
    signal: options.signal,
  });

  if (!response.ok) {
    throw httpErrorFrom(response.status, await response.text());
  }
}
