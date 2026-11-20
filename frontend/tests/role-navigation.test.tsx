import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { pathRef } = vi.hoisted(() => ({ pathRef: { current: "/overview" } }));

vi.mock("next/navigation", () => ({
  usePathname: () => pathRef.current,
  // The header renders the signed-in user menu, which needs a router.
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

import { AppShell } from "../src/components/layout/AppShell";
import { RequirePermission } from "../src/components/auth/RequirePermission";
import { visibleNavItems } from "../src/lib/navigation";
import { USER_ROLES } from "../src/lib/api/types";
import { withPendingSession, withRole } from "./helpers/auth";

/**
 * Role-aware chrome: what each role can see, and what happens to a page guard
 * when the role is unknown or the identity is unresolved.
 *
 * Hiding an entry is an affordance decision — the backend still authorizes the
 * page's requests — so these tests assert *visibility*, and the fail-closed
 * cases, not access control on the wire.
 */

const EXPECTED_HREFS = [
  "/overview",
  "/employees",
  "/attendance",
  "/system-status",
];

const primaryNav = () => screen.getByRole("navigation", { name: "Primary" });

const linksOf = () =>
  within(primaryNav())
    .queryAllByRole("link")
    .map((link) => link.getAttribute("href"));

describe("navigation per role", () => {
  beforeEach(() => {
    pathRef.current = "/overview";
  });

  for (const role of USER_ROLES) {
    it(`shows the four readable areas to ${role}`, () => {
      render(
        withRole(
          role,
          <AppShell>
            <h1>Section content</h1>
          </AppShell>,
        ),
      );

      // Every role holds `employees:read` and `attendance:read`, so the four
      // existing areas are visible to all of them.
      expect(linksOf()).toEqual(EXPECTED_HREFS);
    });
  }

  it("marks the active route for a restricted role too", () => {
    pathRef.current = "/attendance";
    render(
      withRole(
        "Auditor",
        <AppShell>
          <h1>Section content</h1>
        </AppShell>,
      ),
    );

    const current = within(primaryNav())
      .getAllByRole("link")
      .filter((link) => link.getAttribute("aria-current") === "page");
    expect(current).toHaveLength(1);
    expect(current[0]?.textContent).toBe("Attendance");
  });

  it("shows no gated entry before the identity resolves", () => {
    render(
      withPendingSession(
        <AppShell>
          <h1>Section content</h1>
        </AppShell>,
      ),
    );

    expect(linksOf()).toEqual([]);
  });

  it("shows no gated entry without an auth context at all", () => {
    render(
      <AppShell>
        <h1>Section content</h1>
      </AppShell>,
    );

    expect(linksOf()).toEqual([]);
  });
});

describe("visibleNavItems", () => {
  it("keeps only permission-free entries for an unknown role", () => {
    const items = visibleNavItems("Guest");
    expect(items.map((item) => item.href)).toEqual(["/system-status"]);
  });

  it("keeps only permission-free entries for a missing role", () => {
    expect(visibleNavItems(null).map((item) => item.href)).toEqual([
      "/system-status",
    ]);
  });

  it("never hides an area the role can read", () => {
    for (const role of USER_ROLES) {
      expect(visibleNavItems(role).map((item) => item.href)).toEqual(
        EXPECTED_HREFS,
      );
    }
  });
});

describe("RequirePermission", () => {
  it("renders the page for a role that holds the permission", () => {
    for (const role of ["Admin", "Auditor"] as const) {
      const { unmount } = render(
        withRole(
          role,
          <RequirePermission permission="audit:read">
            <h1>Audit journal</h1>
          </RequirePermission>,
        ),
      );
      expect(
        screen.getByRole("heading", { name: "Audit journal" }),
      ).toBeDefined();
      unmount();
    }
  });

  it("shows one access-denied screen to a role without the permission", () => {
    for (const role of ["HR Manager", "Operator"] as const) {
      const { unmount } = render(
        withRole(
          role,
          <RequirePermission permission="audit:read">
            <h1>Audit journal</h1>
          </RequirePermission>,
        ),
      );

      expect(screen.queryByRole("heading", { name: "Audit journal" })).toBeNull();
      expect(
        screen.getByRole("heading", { name: "Access denied" }),
      ).toBeDefined();
      expect(screen.getByRole("alert").textContent).toContain(
        "You do not have permission to perform this action.",
      );
      // The session is not implicated, and no retry is offered.
      expect(screen.getByText(/session is still active/i)).toBeDefined();
      expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
      expect(
        screen.getByRole("link", { name: /back to overview/i }).getAttribute("href"),
      ).toBe("/overview");
      unmount();
    }
  });

  it("renders no protected content while the identity is unresolved", () => {
    render(
      withPendingSession(
        <RequirePermission permission="audit:read">
          <h1>Audit journal</h1>
        </RequirePermission>,
      ),
    );

    expect(screen.queryByRole("heading", { name: "Audit journal" })).toBeNull();
    expect(screen.getByRole("status").textContent).toContain(
      "Checking your access",
    );
  });

  it("denies an unknown role (fail closed)", () => {
    render(
      withRole(
        // A role string outside the approved four cannot reach this through
        // `/me`, which refuses it; the guard still fails closed if it did.
        "Guest" as never,
        <RequirePermission permission="audit:read">
          <h1>Audit journal</h1>
        </RequirePermission>,
      ),
    );

    expect(screen.queryByRole("heading", { name: "Audit journal" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Access denied" })).toBeDefined();
  });
});
