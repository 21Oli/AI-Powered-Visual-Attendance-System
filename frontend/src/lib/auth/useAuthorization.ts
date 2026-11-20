"use client";

/**
 * React integration for the presentation-layer permission model.
 *
 * Reads the role from the existing P5.7 auth state — never from a request, a
 * form field or storage — and answers "should this control be offered?".
 *
 * Fail-closed: while the session is unresolved (`loading`), when it is absent
 * or failed (`unauthenticated` / `error`), and outside a provider entirely,
 * `can()` returns `false` and `ready` is `false`. Callers therefore never offer
 * a privileged action before the identity is known.
 */

import { useMemo } from "react";

import { roleHasPermission } from "../authz/permissions";
import type { Permission } from "../authz/permissions";
import { useOptionalAuth } from "./AuthProvider";

export interface Authorization {
  /** `true` only for an authenticated session with a resolved identity. */
  readonly ready: boolean;
  /** The authenticated role string, or `null` before/without a session. */
  readonly role: string | null;
  /** Whether the current role holds `permission`; `false` when not ready. */
  readonly can: (permission: Permission) => boolean;
}

export function useAuthorization(): Authorization {
  const auth = useOptionalAuth();
  const user = auth !== null && auth.status === "authenticated" ? auth.user : null;
  const ready = user !== null;
  const role = user?.role ?? null;

  return useMemo(
    () => ({
      ready,
      role,
      can: (permission: Permission) => ready && roleHasPermission(role, permission),
    }),
    [ready, role],
  );
}
