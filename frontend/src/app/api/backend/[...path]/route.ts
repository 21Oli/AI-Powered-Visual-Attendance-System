import {
  PROXY_MAX_BODY_BYTES,
  PROXY_REQUEST_TIMEOUT_MS,
  allowedMethodsForPath,
  buildUpstreamUrl,
  matchProxyRoute,
  proxyErrorResponse,
  resolveBackendBaseUrl,
  validateQuery,
} from "../../../../lib/server/proxy";
import type { ProxyMethod } from "../../../../lib/server/proxy";

/**
 * Same-origin backend proxy.
 *
 * The browser talks only to `/api/backend/...`; this handler forwards an
 * allowlisted method/path pair to the loopback FastAPI service using the
 * server-only `BACKEND_BASE_URL`. It never accepts a destination from the
 * client, never follows redirects, never adds CORS headers, and never logs a
 * request body, employee record or attendance payload.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface RouteContext {
  readonly params: Promise<{ readonly path?: readonly string[] }>;
}

function isTimeout(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === "TimeoutError" || error.name === "AbortError")
  );
}

async function handle(
  method: ProxyMethod,
  request: Request,
  context: RouteContext,
): Promise<Response> {
  const { path } = await context.params;
  const segments = path ?? [];

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

  const headers: Record<string, string> = { accept: "application/json" };
  if (body !== undefined) {
    headers["content-type"] = "application/json";
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

  const text = await upstream.text();
  if (text.trim().length === 0) {
    return proxyErrorResponse(
      502,
      "BACKEND_EMPTY_RESPONSE",
      "The dashboard backend returned an empty response.",
    );
  }

  return new Response(text, {
    status: upstream.status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

async function guarded(
  method: ProxyMethod,
  request: Request,
  context: RouteContext,
): Promise<Response> {
  try {
    return await handle(method, request, context);
  } catch {
    return proxyErrorResponse(
      500,
      "PROXY_INTERNAL_ERROR",
      "The dashboard could not complete this request.",
    );
  }
}

export async function GET(request: Request, context: RouteContext) {
  return guarded("GET", request, context);
}

export async function POST(request: Request, context: RouteContext) {
  return guarded("POST", request, context);
}

export async function PATCH(request: Request, context: RouteContext) {
  return guarded("PATCH", request, context);
}
