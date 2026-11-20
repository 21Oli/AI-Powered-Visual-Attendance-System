import { describe, expect, it } from "vitest";

import {
  PERMISSIONS,
  ROLE_PERMISSIONS,
  isPermission,
  permissionsFor,
  roleHasPermission,
} from "../src/lib/authz/permissions";
import type { Permission } from "../src/lib/authz/permissions";
import { USER_ROLES } from "../src/lib/api/types";

/**
 * The frontend mirror of the P5.4.10 policy is presentation only, so these
 * tests exist to keep it *honest*: the expected matrix below is written out
 * independently of the implementation, and any drift between the two shows up
 * here rather than as a hidden or wrongly-offered control.
 */

const EXPECTED: Record<string, Record<Permission, boolean>> = {
  Admin: {
    "employees:read": true,
    "employees:create": true,
    "employees:update": true,
    "employees:deactivate": true,
    "attendance:read": true,
    "attendance:checkout": true,
    "audit:read": true,
    "users:manage": true,
  },
  "HR Manager": {
    "employees:read": true,
    "employees:create": true,
    "employees:update": true,
    "employees:deactivate": false,
    "attendance:read": true,
    "attendance:checkout": true,
    "audit:read": false,
    "users:manage": false,
  },
  Operator: {
    "employees:read": true,
    "employees:create": false,
    "employees:update": false,
    "employees:deactivate": false,
    "attendance:read": true,
    "attendance:checkout": true,
    "audit:read": false,
    "users:manage": false,
  },
  Auditor: {
    "employees:read": true,
    "employees:create": false,
    "employees:update": false,
    "employees:deactivate": false,
    "attendance:read": true,
    "attendance:checkout": false,
    "audit:read": true,
    "users:manage": false,
  },
};

describe("role/permission matrix", () => {
  it("covers exactly the four approved roles", () => {
    expect([...USER_ROLES]).toEqual([
      "Admin",
      "HR Manager",
      "Operator",
      "Auditor",
    ]);
    expect(Object.keys(ROLE_PERMISSIONS).sort()).toEqual(
      [...USER_ROLES].sort(),
    );
  });

  it("defines exactly the eight backend permissions", () => {
    expect([...PERMISSIONS]).toEqual([
      "employees:read",
      "employees:create",
      "employees:update",
      "employees:deactivate",
      "attendance:read",
      "attendance:checkout",
      "audit:read",
      "users:manage",
    ]);
  });

  for (const role of USER_ROLES) {
    for (const permission of PERMISSIONS) {
      const expected = EXPECTED[role][permission];
      it(`${expected ? "allows" : "denies"} ${role} → ${permission}`, () => {
        expect(roleHasPermission(role, permission)).toBe(expected);
        expect(permissionsFor(role).includes(permission)).toBe(expected);
      });
    }
  }

  it("grants every permission to at least one role", () => {
    for (const permission of PERMISSIONS) {
      const holders = USER_ROLES.filter((role) =>
        roleHasPermission(role, permission),
      );
      expect(holders.length).toBeGreaterThan(0);
    }
  });

  it("gives Admin every permission and no role implicit inheritance", () => {
    expect(permissionsFor("Admin")).toHaveLength(PERMISSIONS.length);
    // A role is a flat set: HR Manager holds no `employees:deactivate` merely
    // because it holds `employees:update`.
    expect(roleHasPermission("HR Manager", "employees:deactivate")).toBe(false);
    expect(roleHasPermission("Auditor", "attendance:checkout")).toBe(false);
    expect(roleHasPermission("Operator", "audit:read")).toBe(false);
  });
});

describe("fail-closed lookups", () => {
  it("grants nothing to a missing, unknown or non-string role", () => {
    const unknownRoles: unknown[] = [
      null,
      undefined,
      "",
      "admin",
      "ADMIN",
      "Superuser",
      "Admin ",
      0,
      true,
      {},
      ["Admin"],
      { role: "Admin" },
    ];

    for (const role of unknownRoles) {
      expect(permissionsFor(role)).toEqual([]);
      expect(roleHasPermission(role, "employees:read")).toBe(false);
      expect(roleHasPermission(role, "users:manage")).toBe(false);
    }
  });

  it("denies an unknown or malformed permission even for Admin", () => {
    const unknownPermissions: unknown[] = [
      null,
      undefined,
      "",
      "employees:delete",
      "admin:all",
      "*",
      "EMPLOYEES:READ",
      42,
      {},
    ];

    for (const permission of unknownPermissions) {
      expect(isPermission(permission)).toBe(false);
      expect(roleHasPermission("Admin", permission)).toBe(false);
    }
  });

  it("recognizes the eight known permission names", () => {
    for (const permission of PERMISSIONS) {
      expect(isPermission(permission)).toBe(true);
    }
  });

  it("returns the same answer on repeated calls (no hidden state)", () => {
    const first = permissionsFor("Auditor");
    const second = permissionsFor("Auditor");
    expect(first).toEqual(second);
    expect(first).toEqual(["employees:read", "attendance:read", "audit:read"]);
  });
});
