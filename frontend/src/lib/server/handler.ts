import {
  PROXY_MAX_BODY_BYTES,
  PROXY_REQUEST_TIMEOUT_MS,
  allowedMethodsForPath,
  buildUpstreamUrl,
  extractSessionCookie,
  matchProxyRoute,
  proxyErrorResponse,
  resolveBackendBaseUrl,
  upstreamSetCookieHeaders,
  validateQuery,
} from "./proxy";
import type { ProxyMethod } from "./proxy";

/**
 * The one same-origin proxy implementation.
 *
 * Both entry points use it: `/api/backend/...` (the Phase 4 dashboard proxy)
 * and `/api/v1/auth/...` (the P5.4.8 authentication boundary, whose URL path
 * must live under `/api/v1` so the backend's `Path=/api/v1` session cookie is
 * actually sent by the browser). The allowlist, query rules, identifier
 * validation and upstream construction are the same strict table in both
 * cases — there is no second, looser policy.
 *
 * It never accepts a destination from the client, never follows redirects,
 * never adds CORS headers, and never logs a body, cookie, token or payload.
 */

interface RouteContext {
  readonly path?: readonly string[];
}

function isTimeout(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === "TimeoutError" || error.name === "AbortError")
  );
}

function withSetCookies(
  response: Response,
  upstream: Response,
  forwardsSetCookie: boolean,
): Response {
  if (!forwardsSetCookie) {
    return response;
  }
  for (const value of upstreamSetCookieHeaders(upstream.headers)) {
    response.headers.append("set-cookie", value);
  }
  return response;
}

async function handle(
  method: ProxyMethod,
  request: Request,
  segments: readonly string[],
): Promise<Response> {
  const matched = matchProxyRoute(method, segments);
  if (matched === null) {
    const allowed = allowedMethodsForPath(segments);
    if (allowed.length > 0) {
      return proxyErrorResponse(
        405,
        "PROXY_METHOD_NOT_ALLOWED",
        "This route does not accept that method.",
        { allow: allowed.join(", ") },
      );
    }
    return proxyErrorResponse(
      404,
      "PROXY_ROUTE_NOT_ALLOWED",
      "That backend route is not available through this dashboard.",
    );
  }

  const query = validateQuery(matched, new URL(request.url).searchParams);
  if (!query.ok) {
    return proxyErrorResponse(400, query.code, query.message);
  }

  const configuration = resolveBackendBaseUrl({
    BACKEND_BASE_URL: process.env.BACKEND_BASE_URL,
  });
  if (!configuration.ok) {
    return proxyErrorResponse(503, configuration.code, configuration.message);
  }

  let body: string | undefined;
  if (matched.acceptsBody) {
    const raw = await request.text();
    if (raw.trim().length > 0) {
      if (raw.length > PROXY_MAX_BODY_BYTES) {
        return proxyErrorResponse(
          413,
          "PROXY_REQUEST_INVALID",
          "The request body is too large.",
        );
      }
      body = raw;
    }
  }

  // Explicit header allowlist: nothing else from the browser is forwarded.
  const headers: Record<string, string> = { accept: "application/json" };
  if (body !== undefined) {
    headers["content-type"] = "application/json";
  }
  if (matched.forwardsOrigin) {
    // Login/logout only: forward the browser's Origin exactly as received. It
    // is never synthesized — an absent Origin stays absent so the backend
    // rejects the request — and no other client header is passed through.
    const origin = request.headers.get("origin");
    if (origin !== null) {
      headers.origin = origin;
    }
  }
  if (matched.forwardsSessionCookie) {
    // `/me` and logout only: exactly the `va_session` cookie, never the rest.
    const cookie = extractSessionCookie(request.headers.get("cookie"));
    if (cookie !== null) {
      headers.cookie = cookie;
    }
  }

  let upstream: Response;
  try {
    upstream = await fetch(
      buildUpstreamUrl(configuration.baseUrl, matched, query.search),
      {
        method,
        headers,
        body,
        cache: "no-store",
        // A redirect could leave the trusted backend origin, so it is never
        // followed: the 3xx is reported as a sanitized failure instead.
        redirect: "manual",
        signal: AbortSignal.timeout(PROXY_REQUEST_TIMEOUT_MS),
      },
    );
  } catch (error) {
    if (isTimeout(error)) {
      return proxyErrorResponse(
        504,
        "BACKEND_TIMEOUT",
        "The dashboard backend did not respond in time.",
      );
    }
    return proxyErrorResponse(
      502,
      "BACKEND_UNAVAILABLE",
      "The dashboard backend could not be reached.",
    );
  }

  if (upstream.status >= 300 && upstream.status < 400) {
    return proxyErrorResponse(
      502,
      "BACKEND_REDIRECT_REFUSED",
      "The dashboard backend returned a redirect, which is not followed.",
    );
  }

  // A 204 (logout) has no body by contract: it must stay bodyless, and reading
  // it as JSON would turn a successful sign-out into a gateway error.
  if (upstream.status === 204 || upstream.status === 205) {
    return withSetCookies(
      new Response(null, {
        status: upstream.status,
        headers: { "cache-control": "no-store" },
      }),
      upstream,
      matched.forwardsSetCookie,
    );
  }

  const text = await upstream.text();
  if (text.trim().length === 0) {
    return proxyErrorResponse(
      502,
      "BACKEND_EMPTY_RESPONSE",
      "The dashboard backend returned an empty response.",
    );
  }

  // The body is re-served as-is (status and JSON payload preserved); the only
  // response header carried back is the backend's `Set-Cookie`, and only for
  // the two routes that establish or end a session. Browser session handling
  // stays HTTP-only: the cookie is never converted into a JSON field.
  return withSetCookies(
    new Response(text, {
      status: upstream.status,
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
      },
    }),
    upstream,
    matched.forwardsSetCookie,
  );
}

/** Run the proxy, converting an unexpected fault into a sanitized 500. */
export async function proxyBackendRequest(
  method: ProxyMethod,
  request: Request,
  context: RouteContext,
): Promise<Response> {
  try {
    return await handle(method, request, context.path ?? []);
  } catch {
    return proxyErrorResponse(
      500,
      "PROXY_INTERNAL_ERROR",
      "The dashboard could not complete this request.",
    );
  }
}
