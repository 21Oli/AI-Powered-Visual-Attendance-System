import { proxyBackendRequest } from "../../../../lib/server/handler";
import type { ProxyMethod } from "../../../../lib/server/proxy";

/**
 * Same-origin backend proxy (`/api/backend/...`).
 *
 * The browser talks only to this route; the handler forwards an allowlisted
 * method/path pair to the loopback FastAPI service using the server-only
 * `BACKEND_BASE_URL`. It never accepts a destination from the client, never
 * follows redirects, never adds CORS headers, and never logs a request body,
 * employee record or attendance payload. The policy itself lives in
 * `src/lib/server/proxy.ts` and the transport in `src/lib/server/handler.ts`,
 * both shared with the `/api/v1/auth/...` boundary.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface RouteContext {
  readonly params: Promise<{ readonly path?: readonly string[] }>;
}

async function guarded(
  method: ProxyMethod,
  request: Request,
  context: RouteContext,
): Promise<Response> {
  const { path } = await context.params;
  return proxyBackendRequest(method, request, { path });
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
