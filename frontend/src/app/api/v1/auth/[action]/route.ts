import { proxyBackendRequest } from "../../../../../lib/server/handler";
import type { ProxyMethod } from "../../../../../lib/server/proxy";

/**
 * Authentication BFF boundary (`/api/v1/auth/...`, P5.4.8).
 *
 * The browser calls these same-origin URLs, never FastAPI. The path lives
 * under `/api/v1` deliberately: the backend's session cookie is issued with
 * `Path=/api/v1`, so only requests to `/api/v1/...` carry it — a `/api/backend`
 * URL would never receive the cookie, and a broader cookie path would weaken
 * the approved policy. The handler reuses the exact same allowlist and
 * transport as the dashboard proxy: only `POST /auth/login`, `GET /auth/me`
 * and `POST /auth/logout` exist, decided by the shared table rather than by
 * anything the client sends.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Backend prefix the auth actions hang under (the allowlist keys on it). */
const AUTH_SEGMENTS = ["api", "v1", "auth"] as const;

interface RouteContext {
  readonly params: Promise<{ readonly action?: string }>;
}

async function forward(
  method: ProxyMethod,
  request: Request,
  context: RouteContext,
): Promise<Response> {
  const { action } = await context.params;
  const segments =
    typeof action === "string" && action.length > 0
      ? [...AUTH_SEGMENTS, action]
      : [...AUTH_SEGMENTS];
  return proxyBackendRequest(method, request, { path: segments });
}

export async function GET(request: Request, context: RouteContext) {
  return forward("GET", request, context);
}

export async function POST(request: Request, context: RouteContext) {
  return forward("POST", request, context);
}
