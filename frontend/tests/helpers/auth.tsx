import type { ReactNode } from "react";

import type {
  AuthContextValue,
  AuthStatus,
} from "../../src/lib/auth/AuthProvider";
import { AuthContext } from "../../src/lib/auth/AuthProvider";
import type { AuthUser, UserRole } from "../../src/lib/api/types";

/**
 * Test fixtures for the authorization layer.
 *
 * A context value is supplied directly instead of mocking `/me`, so a component
 * test states the role it is exercising in one line and performs no session
 * request. The helper constructs exactly the public `AuthContextValue`, so the
 * tests cannot reach past the contract the components use.
 */

export function authUser(role: UserRole): AuthUser {
  return {
    user_id: "user-1",
    username: "ada",
    role,
    is_active: true,
  };
}

export function authValue(
  role: UserRole,
  status: AuthStatus = "authenticated",
): AuthContextValue {
  return {
    status,
    user: status === "authenticated" ? authUser(role) : null,
    error: null,
    notice: null,
    signIn: async () => authUser(role),
    signOut: async () => undefined,
    refresh: () => undefined,
  };
}

/** Wrap `ui` in an auth context for `role`. */
export function withRole(role: UserRole, ui: ReactNode) {
  return (
    <AuthContext.Provider value={authValue(role)}>{ui}</AuthContext.Provider>
  );
}

/** Wrap `ui` in a context that is still checking the session. */
export function withPendingSession(ui: ReactNode) {
  return (
    <AuthContext.Provider value={authValue("Admin", "loading")}>
      {ui}
    </AuthContext.Provider>
  );
}
