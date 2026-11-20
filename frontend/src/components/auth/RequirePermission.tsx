"use client";

import type { ReactNode } from "react";

import { useAuthorization } from "../../lib/auth/useAuthorization";
import type { Permission } from "../../lib/authz/permissions";
import { AccessDenied, AuthPending } from "./AuthScreens";

/**
 * Renders `children` only when the signed-in role holds `permission`.
 *
 * This is a presentation guard, not enforcement: it keeps a restricted page
 * from rendering for a role the backend would refuse, so the visitor sees one
 * consistent access-denied screen instead of a page full of failed requests.
 * Every request the page would make is still authorized server-side.
 *
 * `RequireAuth` already sits above it, so this only has to handle the two cases
 * it owns: the identity is not resolved yet (render a status screen, never the
 * page) and the role is permitted or not.
 */
export function RequirePermission({
  permission,
  children,
}: {
  permission: Permission;
  children: ReactNode;
}) {
  const { ready, can } = useAuthorization();

  if (!ready) {
    return <AuthPending label="Checking your access…" />;
  }
  if (!can(permission)) {
    return <AccessDenied />;
  }
  return <>{children}</>;
}
