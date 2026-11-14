/**
 * Server-only proxy policy for `/api/backend/...`.
 *
 * Kept free of Next.js imports so the whole allowlist, identifier validation,
 * query rules and upstream-URL construction can be unit-tested directly. The
 * Route Handler is a thin adapter over these functions.
 */

export type ProxyMethod = "GET" | "POST" | "PATCH";

export const PROXY_REQUEST_TIMEOUT_MS = 8_000;
export const PROXY_MAX_BODY_BYTES = 4_096;

export const MAX_EMPLOYEE_ID_LENGTH = 128;

/** The one session cookie the browser may carry; nothing else is forwarded. */
export const SESSION_COOKIE_NAME = "va_session";

/** Sanity ceiling for a forwarded session-cookie value (the token is 43 chars). */
export const MAX_SESSION_COOKIE_BYTES = 512;

/**
 * An identifier that can be carried safely in a single path segment.
 *
 * Deliberately permissive about the characters the backend itself allows
 * (it accepts any non-empty string) while refusing everything that could make
 * a segment ambiguous or escape its slot: separators, encoded separators,
 * query/fragment markers, control characters, surrounding whitespace, and the
 * `.` / `..` traversal names.
 */
export function isSafeEmployeeId(value: string): boolean {
  if (value.length === 0 || value.length > MAX_EMPLOYEE_ID_LENGTH) {
    return false;
  }
  if (value !== value.trim()) {
    return false;
  }
  if (value === "." || value === "..") {
    return false;
  }
  if (/[/\\?#%]/.test(value)) {
    return false;
  }
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0;
    if (code < 0x20 || code === 0x7f) {
      return false;
    }
  }
  return true;
}

interface RouteRule {
  readonly method: ProxyMethod;
  /** `:employee_id` marks the one variable segment. */
  readonly pattern: readonly string[];
  readonly allowsEmployeeQuery: boolean;
  /** Overrides the default body rule (login is a `POST` with a body). */
  readonly acceptsBody?: boolean;
  /** Login/logout forward the browser's `Origin` (P5.4.5/P5.4.8). */
  readonly forwardsOrigin?: boolean;
  /** `/me` and logout forward only the `va_session` cookie (P5.4.8). */
  readonly forwardsSessionCookie?: boolean;
  /** Login/logout pass the backend's `Set-Cookie` back to the browser. */
  readonly forwardsSetCookie?: boolean;
}

/**
 * The approved allowlist: the eight backend business routes plus the three
 * authentication routes (`login`, `me`, `logout`). Nothing else is reachable
 * through the proxy — there is no wildcard, no arbitrary method and no
 * client-supplied upstream URL anywhere in this table.
 */
const ROUTE_RULES: readonly RouteRule[] = [
  { method: "GET", pattern: ["api", "v1", "health"], allowsEmployeeQuery: false },
  {
    method: "POST",
    pattern: ["api", "v1", "auth", "login"],
    allowsEmployeeQuery: false,
    acceptsBody: true,
    forwardsOrigin: true,
    forwardsSetCookie: true,
  },
  {
    method: "GET",
    pattern: ["api", "v1", "auth", "me"],
    allowsEmployeeQuery: false,
    forwardsSessionCookie: true,
  },
  {
    method: "POST",
    pattern: ["api", "v1", "auth", "logout"],
    allowsEmployeeQuery: false,
    forwardsOrigin: true,
    forwardsSessionCookie: true,
    forwardsSetCookie: true,
  },
  { method: "POST", pattern: ["api", "v1", "employees"], allowsEmployeeQuery: false },
  {
    method: "GET",
    pattern: ["api", "v1", "employees"],
    allowsEmployeeQuery: true,
  },
  {
    method: "GET",
    pattern: ["api", "v1", "employees", ":employee_id"],
    allowsEmployeeQuery: false,
  },
  {
    method: "PATCH",
    pattern: ["api", "v1", "employees", ":employee_id"],
    allowsEmployeeQuery: false,
  },
  {
    method: "POST",
    pattern: ["api", "v1", "employees", ":employee_id", "deactivate"],
    allowsEmployeeQuery: false,
  },
  {
    method: "GET",
    pattern: ["api", "v1", "attendance"],
    allowsEmployeeQuery: false,
  },
  {
    method: "POST",
    pattern: ["api", "v1", "attendance", ":employee_id", "checkout"],
    allowsEmployeeQuery: false,
  },
];

export interface MatchedRoute {
  readonly method: ProxyMethod;
  /** Decoded segments, with the identifier already validated. */
  readonly segments: readonly string[];
  readonly employeeId: string | null;
  readonly allowsEmployeeQuery: boolean;
  /** Only POST /employees, PATCH /employees/{id} and login carry a body. */
  readonly acceptsBody: boolean;
  /** Login and logout forward the browser's `Origin` header unchanged. */
  readonly forwardsOrigin: boolean;
  /** `/me` and logout forward only the `va_session` cookie. */
  readonly forwardsSessionCookie: boolean;
  /** Login and logout pass the backend's `Set-Cookie` to the browser. */
  readonly forwardsSetCookie: boolean;
}

function matchesPattern(
  pattern: readonly string[],
  segments: readonly string[],
): boolean {
  if (pattern.length !== segments.length) {
    return false;
  }
  return pattern.every((part, index) => {
    const segment = segments[index];
    return part === ":employee_id"
      ? isSafeEmployeeId(segment)
      : part === segment;
  });
}

/** Match an approved method/path pair, or `null` when nothing allows it. */
export function matchProxyRoute(
  method: string,
  segments: readonly string[],
): MatchedRoute | null {
  const normalizedMethod = method.toUpperCase();
  for (const rule of ROUTE_RULES) {
    if (rule.method !== normalizedMethod) {
      continue;
    }
    if (!matchesPattern(rule.pattern, segments)) {
      continue;
    }
    const idIndex = rule.pattern.indexOf(":employee_id");
    const employeeId = idIndex === -1 ? null : segments[idIndex];
    return {
      method: rule.method,
      segments,
      employeeId,
      allowsEmployeeQuery: rule.allowsEmployeeQuery,
      acceptsBody:
        rule.acceptsBody ??
        ((rule.method === "POST" && rule.pattern.length === 3) ||
          (rule.method === "PATCH" && rule.pattern.length === 4)),
      forwardsOrigin: rule.forwardsOrigin === true,
      forwardsSessionCookie: rule.forwardsSessionCookie === true,
      forwardsSetCookie: rule.forwardsSetCookie === true,
    };
  }
  return null;
}

/** Methods the allowlist defines for a path, used to answer 405 correctly. */
export function allowedMethodsForPath(
  segments: readonly string[],
): ProxyMethod[] {
  const methods: ProxyMethod[] = [];
  for (const rule of ROUTE_RULES) {
    if (
      matchesPattern(rule.pattern, segments) &&
      !methods.includes(rule.method)
    ) {
      methods.push(rule.method);
    }
  }
  return methods;
}

export type QueryResult =
  | { readonly ok: true; readonly search: string }
  | { readonly ok: false; readonly code: string; readonly message: string };

const LIMIT_PATTERN = /^[1-9][0-9]{0,3}$/;

/**
 * Query handling: employee paging is the only accepted query surface.
 * Everything else must arrive with no query at all, so nothing is silently
 * forwarded to a route that does not understand it.
 */
export function validateQuery(
  matched: MatchedRoute,
  params: URLSearchParams,
): QueryResult {
  if (!matched.allowsEmployeeQuery) {
    const [firstKey] = [...params.keys()];
    if (firstKey !== undefined) {
      return {
        ok: false,
        code: "PROXY_REQUEST_INVALID",
        message: "This route does not accept query parameters.",
      };
    }
    return { ok: true, search: "" };
  }

  const normalized = new URLSearchParams();
  for (const [key, value] of params.entries()) {
    if (key === "after") {
      if (!isSafeEmployeeId(value)) {
        return {
          ok: false,
          code: "PROXY_REQUEST_INVALID",
          message: "The pagination cursor is not valid.",
        };
      }
      normalized.set("after", value);
      continue;
    }
    if (key === "limit") {
      if (!LIMIT_PATTERN.test(value)) {
        return {
          ok: false,
          code: "PROXY_REQUEST_INVALID",
          message: "The page size must be a positive integer up to 100.",
        };
      }
      normalized.set("limit", value);
      continue;
    }
    return {
      ok: false,
      code: "PROXY_REQUEST_INVALID",
      message: "Unsupported query parameter.",
    };
  }

  return { ok: true, search: normalized.toString() };
}

export type BaseUrlResult =
  | { readonly ok: true; readonly baseUrl: string }
  | { readonly ok: false; readonly code: string; readonly message: string };

/**
 * Resolve and validate the server-only backend origin.
 *
 * Accepts only an absolute http(s) origin: no credentials, no query, no
 * fragment and no path, so a stray value cannot redirect traffic elsewhere or
 * smuggle components into every request.
 */
export function resolveBackendBaseUrl(env: {
  readonly BACKEND_BASE_URL?: string;
}): BaseUrlResult {
  const raw = (env.BACKEND_BASE_URL ?? "").trim();
  if (raw.length === 0) {
    return {
      ok: false,
      code: "BACKEND_NOT_CONFIGURED",
      message: "The dashboard backend endpoint is not configured.",
    };
  }

  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return {
      ok: false,
      code: "BACKEND_CONFIGURATION_INVALID",
      message: "The configured backend endpoint is not a valid URL.",
    };
  }

  const invalid = {
    ok: false as const,
    code: "BACKEND_CONFIGURATION_INVALID",
    message: "The configured backend endpoint is not acceptable.",
  };

  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return invalid;
  }
  if (url.username !== "" || url.password !== "") {
    return invalid;
  }
  if (url.search !== "" || url.hash !== "") {
    return invalid;
  }
  if (url.pathname !== "/" && url.pathname !== "") {
    return invalid;
  }

  return { ok: true, baseUrl: `${url.protocol}//${url.host}` };
}

/**
 * Build the upstream URL. Identifiers are re-encoded exactly once, and the path
 * is rebuilt from the matched segments — never from the raw request URL — so a
 * client cannot influence the destination.
 */
export function buildUpstreamUrl(
  baseUrl: string,
  matched: MatchedRoute,
  search: string,
): string {
  const path = matched.segments.map((segment) => encodeURIComponent(segment)).join("/");
  return `${baseUrl}/${path}${search.length > 0 ? `?${search}` : ""}`;
}

/**
 * Return only the `va_session` cookie from a browser `Cookie` header.
 *
 * Every other cookie is dropped: the backend never needs them, and forwarding
 * an unrelated credential would widen what a compromised backend sees. The
 * value is syntax-checked (single occurrence, no separators/controls, bounded
 * length) so a malformed header cannot inject extra upstream headers; an
 * absent, empty, duplicated or malformed value simply yields `null` and the
 * backend answers with its own generic 401.
 */
export function extractSessionCookie(
  cookieHeader: string | null | undefined,
): string | null {
  if (typeof cookieHeader !== "string" || cookieHeader.length === 0) {
    return null;
  }
  const values: string[] = [];
  for (const part of cookieHeader.split(";")) {
    const pair = part.trim();
    const separator = pair.indexOf("=");
    if (separator <= 0) {
      continue;
    }
    if (pair.slice(0, separator).trim() !== SESSION_COOKIE_NAME) {
      continue;
    }
    const value = pair.slice(separator + 1).trim();
    if (
      value.length === 0 ||
      value.length > MAX_SESSION_COOKIE_BYTES ||
      !/^[A-Za-z0-9._~+/=-]+$/.test(value)
    ) {
      return null;
    }
    values.push(value);
  }
  if (values.length !== 1) {
    return null;
  }
  return `${SESSION_COOKIE_NAME}=${values[0]}`;
}

/**
 * The upstream `Set-Cookie` values, never comma-joined.
 *
 * `Headers.getSetCookie()` is the only API that keeps multiple cookie headers
 * separate (a cookie's `Expires` attribute legitimately contains a comma), so
 * it is used when available; the single-value fallback is for runtimes that
 * lack it and cannot corrupt a value the way comma-splitting would.
 */
export function upstreamSetCookieHeaders(headers: Headers): string[] {
  const withCookies = headers as Headers & { getSetCookie?: () => string[] };
  if (typeof withCookies.getSetCookie === "function") {
    return withCookies.getSetCookie();
  }
  const single = headers.get("set-cookie");
  return single === null ? [] : [single];
}

export function proxyErrorResponse(
  status: number,
  code: string,
  message: string,
  extraHeaders?: Record<string, string>,
): Response {
  return new Response(JSON.stringify({ error: { code, message } }), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...extraHeaders,
    },
  });
}
