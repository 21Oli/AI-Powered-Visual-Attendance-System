import { API_PATHS, requestJson, requestVoid } from "./client";
import { parseAuthUser, parseLoginResponse } from "./types";
import type { AuthUser } from "./types";

/**
 * Authentication through the same-origin BFF boundary (P5.4.8).
 *
 * Nothing here handles the session token: the browser stores the backend's
 * `va_session` cookie (HttpOnly, `Path=/api/v1`, `SameSite=Lax`, no `Domain`)
 * and sends it automatically to `/api/v1/...`. The token is never returned in
 * a JSON body, never read by this module, and never placed in React state,
 * `localStorage`, `sessionStorage`, IndexedDB or a script-readable cookie.
 *
 * The role returned by `/me` is display data only: it is not an authorization
 * decision — the backend remains the authority for every protected operation.
 */

export interface LoginCredentials {
  readonly username: string;
  readonly password: string;
}

/**
 * `POST /api/v1/auth/login`.
 *
 * Resolves with the credential-free identity from the login response; the
 * session cookie arrives as a `Set-Cookie` header the browser applies, so it
 * is not part of the resolved value.
 */
export async function login(
  credentials: LoginCredentials,
  signal?: AbortSignal,
): Promise<AuthUser> {
  const response = await requestJson({
    path: API_PATHS.login,
    direct: true,
    method: "POST",
    // Exactly the backend contract: no user_id/role/is_active may be supplied.
    body: {
      username: credentials.username,
      password: credentials.password,
    },
    signal,
    parse: parseLoginResponse,
  });
  return response.user;
}

/** `GET /api/v1/auth/me` — the identity of the current session's user. */
export async function fetchCurrentUser(signal?: AbortSignal): Promise<AuthUser> {
  return requestJson({
    path: API_PATHS.me,
    direct: true,
    signal,
    parse: parseAuthUser,
  });
}

/**
 * `POST /api/v1/auth/logout` — 204 with a cookie-clearing `Set-Cookie`.
 *
 * Resolves only for a genuine backend success; a failed sign-out throws a
 * typed `ApiError` instead of being reported as signed out.
 */
export async function logout(signal?: AbortSignal): Promise<void> {
  await requestVoid({
    path: API_PATHS.logout,
    method: "POST",
    signal,
  });
}
