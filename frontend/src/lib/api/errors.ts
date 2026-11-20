import type { FieldError } from "./types";

export type ApiErrorKind = "network" | "http" | "malformed" | "unknown";

export interface ApiErrorInit {
  readonly kind: ApiErrorKind;
  readonly code: string;
  readonly message: string;
  readonly status?: number;
  readonly reason?: string;
  readonly fields?: readonly FieldError[];
}

/**
 * One error type for every failure mode the dashboard can see, with the
 * distinction the UI needs:
 *
 * * `network`   — the request never produced a response.
 * * `http`      — the backend (or the proxy) answered with an error status and
 *                 the documented envelope; `code`/`reason`/`fields` survive.
 * * `malformed` — a 2xx response whose body was not the contracted shape.
 * * `unknown`   — anything else, never rendered verbatim to the user.
 */
export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly code: string;
  readonly status?: number;
  readonly reason?: string;
  readonly fields?: readonly FieldError[];

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = "ApiError";
    this.kind = init.kind;
    this.code = init.code;
    this.status = init.status;
    this.reason = init.reason;
    this.fields = init.fields;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/**
 * The one sentence shown when the backend refuses an action for this role.
 *
 * Fixed text, shared by every surface (page panels, forms, check-out): the
 * backend's own message names neither the role nor the permission, and neither
 * does this.
 */
export const ACCESS_DENIED_MESSAGE =
  "You do not have permission to perform this action.";

/** `403`: authenticated, but the role does not hold the permission. */
export function isForbidden(error: ApiError): boolean {
  return error.kind === "http" && error.status === 403;
}

/** `401`: the session was rejected. Never inferred from a transport failure. */
export function isUnauthorized(error: ApiError): boolean {
  return error.kind === "http" && error.status === 401;
}

/**
 * A service failure the visitor can reasonably retry: unreachable, or 502/503/504.
 *
 * Deliberately excludes 401 and 403 — neither is fixed by trying again, and
 * treating them as "unavailable" would hide an authorization outcome.
 */
export function isUnavailable(error: ApiError): boolean {
  if (error.kind === "network") {
    return true;
  }
  return (
    error.kind === "http" &&
    (error.status === 502 || error.status === 503 || error.status === 504)
  );
}

/** Normalise any thrown value into an `ApiError` without leaking its text. */
export function toApiError(cause: unknown): ApiError {
  if (isApiError(cause)) {
    return cause;
  }
  return new ApiError({
    kind: "unknown",
    code: "UNEXPECTED_ERROR",
    message: "Something went wrong while loading this data.",
  });
}

export function networkError(): ApiError {
  return new ApiError({
    kind: "network",
    code: "NETWORK_UNAVAILABLE",
    message: "The dashboard could not reach its API.",
  });
}

export function malformedError(message: string, code = "MALFORMED_RESPONSE"): ApiError {
  return new ApiError({ kind: "malformed", code, message });
}
