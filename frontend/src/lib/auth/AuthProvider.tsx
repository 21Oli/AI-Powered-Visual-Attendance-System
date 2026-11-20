"use client";

/**
 * Authentication state for the dashboard, built only on the existing P5.4
 * backend session architecture.
 *
 * The session itself is the backend's `va_session` cookie: HttpOnly,
 * `Path=/api/v1`, `SameSite=Lax`, no `Domain`. This provider therefore never
 * sees, stores or forwards a token — it calls `/api/v1/auth/login`,
 * `/api/v1/auth/me` and `/api/v1/auth/logout` through the same-origin BFF and
 * relies on the browser to send the cookie. Nothing is written to
 * `localStorage`, `sessionStorage`, IndexedDB or a script-readable cookie.
 *
 * State model:
 *
 * * `loading`       — the session is being established or re-established.
 * * `authenticated` — `/me` answered with the four-field identity.
 * * `unauthenticated` — `/me` answered `401` (absent, expired or revoked
 *   session), or a sign-out succeeded. This is the only 401-based transition;
 *   a network/5xx failure is never treated as proof of being signed out.
 * * `error`         — the session could not be *verified* (network, 503, or a
 *   malformed response). Protected content stays hidden and the UI offers a
 *   bounded retry instead of redirecting to the login page.
 *
 * Duplicate `/me` requests are suppressed with an in-flight guard, which also
 * covers React StrictMode's double effect invocation.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

import {
  fetchCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
} from "../api/auth";
import { ApiError, toApiError } from "../api/errors";
import type { AuthUser } from "../api/types";

export type AuthStatus = "loading" | "authenticated" | "unauthenticated" | "error";

/** Why the visitor is unauthenticated, when the dashboard knows. */
export type AuthNotice = "expired" | null;

export interface AuthContextValue {
  readonly status: AuthStatus;
  readonly user: AuthUser | null;
  /** The failure that prevented verification; only set while `error`. */
  readonly error: ApiError | null;
  /** `"expired"` after a rejected session, `null` otherwise. */
  readonly notice: AuthNotice;
  /** Authenticate; resolves with the identity or throws the `ApiError`. */
  readonly signIn: (username: string, password: string) => Promise<AuthUser>;
  /** Revoke the current session; throws (leaving state intact) on failure. */
  readonly signOut: () => Promise<void>;
  /** Re-attempt the session check (user-initiated retry only). */
  readonly refresh: () => void;
}

interface AuthState {
  readonly status: AuthStatus;
  readonly user: AuthUser | null;
  readonly error: ApiError | null;
  readonly notice: AuthNotice;
}

const INITIAL_STATE: AuthState = {
  status: "loading",
  user: null,
  error: null,
  notice: null,
};

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * Ask the backend who the session belongs to and translate the answer into a
 * state value. Pure: no React, no storage, no logging.
 *
 * `hadSession` is what makes the expiry notice honest. A `401` on a cold load
 * cannot distinguish "never signed in" from "signed in, cookie since expired
 * or revoked" — the backend deliberately answers both identically. So the
 * notice is only raised when this tab already held an authenticated session;
 * a first visit stays neutral.
 */
async function requestSessionState(hadSession: boolean): Promise<AuthState> {
  try {
    const user = await fetchCurrentUser();
    return { status: "authenticated", user, error: null, notice: null };
  } catch (cause) {
    const error = toApiError(cause);
    if (error.kind === "http" && error.status === 401) {
      return {
        status: "unauthenticated",
        user: null,
        error: null,
        notice: hadSession ? "expired" : null,
      };
    }
    // Network, 5xx or malformed: unknown, which is not "signed out".
    return { status: "error", user: null, error, notice: null };
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(INITIAL_STATE);

  // One session check per provider instance at a time, so StrictMode's double
  // effect and a rapid retry cannot issue duplicate `/me` requests.
  const requestInFlight = useRef(false);
  const mounted = useRef(true);
  // Whether this tab has held an authenticated session; drives the notice.
  const hadSession = useRef(false);

  const applyState = useCallback((next: AuthState) => {
    if (!mounted.current) {
      return;
    }
    hadSession.current = next.status === "authenticated";
    setState(next);
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (requestInFlight.current) {
      return;
    }
    requestInFlight.current = true;
    void requestSessionState(hadSession.current)
      .then(applyState)
      .finally(() => {
        requestInFlight.current = false;
      });
  }, [applyState]);

  const signIn = useCallback(async (username: string, password: string) => {
    const user = await loginRequest({ username, password });
    applyState({ status: "authenticated", user, error: null, notice: null });
    return user;
  }, [applyState]);

  const signOut = useCallback(async () => {
    await logoutRequest();
    applyState({
      status: "unauthenticated",
      user: null,
      error: null,
      notice: null,
    });
  }, [applyState]);

  const refresh = useCallback(() => {
    if (requestInFlight.current) {
      return;
    }
    // A user-initiated retry is the only place that re-enters `loading`; the
    // initial state already is `loading`, so the mount effect never needs to
    // set state synchronously.
    requestInFlight.current = true;
    setState(INITIAL_STATE);
    void requestSessionState(hadSession.current)
      .then(applyState)
      .finally(() => {
        requestInFlight.current = false;
      });
  }, [applyState]);

  const value = useMemo<AuthContextValue>(
    () => ({
      status: state.status,
      user: state.user,
      error: state.error,
      notice: state.notice,
      signIn,
      signOut,
      refresh,
    }),
    [state, signIn, signOut, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/**
 * The auth context, or `null` when rendered outside a provider.
 *
 * Used by chrome that must also render in isolation (the shell's user menu is
 * rendered without a provider in existing component tests). It never makes an
 * authorization decision.
 */
export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext);
}

/** The auth context; throws when the tree is not wrapped in `AuthProvider`. */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error("useAuth must be used inside an AuthProvider.");
  }
  return context;
}
