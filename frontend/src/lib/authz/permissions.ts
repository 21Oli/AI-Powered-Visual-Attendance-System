/**
 * Presentation-layer mirror of the backend authorization policy.
 *
 * The authority is `src/authz.py` (`Permission`, `ROLE_PERMISSIONS`,
 * `has_permission`). This module repeats that matrix so the dashboard can hide
 * controls the backend would refuse — nothing more. It is **not** enforcement:
 * every protected request is still authorized by the backend, and a mistaken
 * entry here can only change what the UI offers, never what the service allows.
 *
 * Fail-closed rules, matching the backend's own:
 *
 * * only the four known role strings grant anything — `null`, `undefined`, an
 *   object or an unknown spelling (`"admin"`, `"Superuser"`) gets nothing;
 * * a permission that is not one of the eight known names is denied;
 * * there is no wildcard, no inheritance and no implicit administrative power.
 *
 * `tests/permissions.test.ts` pins the whole four-by-seven matrix plus the
 * refusal cases, so drift from `src/authz.py` is visible in review rather than
 * discovered in the UI.
 */

import type { UserRole } from "../api/types";

/** The eight permissions, exactly as the backend spells them. */
export const PERMISSIONS = [
  "employees:read",
  "employees:create",
  "employees:update",
  "employees:deactivate",
  "attendance:read",
  "attendance:checkout",
  "audit:read",
  "users:manage",
] as const;

export type Permission = (typeof PERMISSIONS)[number];

/** Which permissions each role holds (P5.4.10 matrix). */
export const ROLE_PERMISSIONS: Readonly<
  Record<UserRole, readonly Permission[]>
> = {
  Admin: PERMISSIONS,
  "HR Manager": [
    "employees:read",
    "employees:create",
    "employees:update",
    "attendance:read",
    "attendance:checkout",
  ],
  Operator: ["employees:read", "attendance:read", "attendance:checkout"],
  Auditor: ["employees:read", "attendance:read", "audit:read"],
};

const KNOWN_PERMISSIONS: ReadonlySet<string> = new Set(PERMISSIONS);

/** Whether `value` is one of the eight permission names. */
export function isPermission(value: unknown): value is Permission {
  return typeof value === "string" && KNOWN_PERMISSIONS.has(value);
}

/**
 * The permissions granted to `role`.
 *
 * The role arrives from `/api/v1/auth/me` as a string (the backend serializes
 * the enum value). Anything that is not one of the four approved strings — and
 * anything that is not a string at all — holds nothing.
 */
export function permissionsFor(role: unknown): readonly Permission[] {
  if (typeof role !== "string") {
    return [];
  }
  const granted = (
    ROLE_PERMISSIONS as Readonly<Record<string, readonly Permission[]>>
  )[role];
  return granted ?? [];
}

/**
 * Whether `role` may perform `permission`, for presentation purposes.
 *
 * Denies unknown roles and unknown permissions. A `false` answer means "do not
 * offer the control"; it never means "the backend would allow it".
 */
export function roleHasPermission(
  role: unknown,
  permission: unknown,
): boolean {
  if (!isPermission(permission)) {
    return false;
  }
  return permissionsFor(role).includes(permission);
}
