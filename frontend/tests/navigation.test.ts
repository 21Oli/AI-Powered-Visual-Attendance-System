import { describe, expect, it } from "vitest";

import {
  DEFAULT_ROUTE,
  NAV_ITEMS,
  findNavItem,
  isActivePath,
} from "../src/lib/navigation";

describe("navigation configuration", () => {
  it("defines the four dashboard areas in order", () => {
    expect(NAV_ITEMS.map((item) => item.label)).toEqual([
      "Overview",
      "Employees",
      "Attendance",
      "System Status",
    ]);
    expect(NAV_ITEMS.map((item) => item.href)).toEqual([
      "/overview",
      "/employees",
      "/attendance",
      "/system-status",
    ]);
  });

  it("uses unique absolute internal routes", () => {
    const hrefs = NAV_ITEMS.map((item) => item.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
    for (const href of hrefs) {
      expect(href.startsWith("/")).toBe(true);
    }
  });

  it("defaults to the overview area", () => {
    expect(DEFAULT_ROUTE).toBe("/overview");
    expect(NAV_ITEMS[0].href).toBe(DEFAULT_ROUTE);
  });

  it("gives every area a purpose and an icon", () => {
    for (const item of NAV_ITEMS) {
      expect(item.purpose.length).toBeGreaterThan(20);
      expect(item.icon.length).toBeGreaterThan(0);
    }
  });

  it("marks exact and nested routes as active", () => {
    expect(isActivePath("/overview", "/overview")).toBe(true);
    expect(isActivePath("/employees/42", "/employees")).toBe(true);
  });

  it("does not mark unrelated routes as active", () => {
    expect(isActivePath("/employees", "/overview")).toBe(false);
    expect(isActivePath("/employees-archive", "/employees")).toBe(false);
    expect(isActivePath("/", "/overview")).toBe(false);
    expect(isActivePath(null, "/employees")).toBe(false);
    expect(isActivePath(undefined, "/employees")).toBe(false);
  });

  it("resolves the area for a route", () => {
    expect(findNavItem("/attendance")?.label).toBe("Attendance");
    expect(findNavItem("/system-status")?.label).toBe("System Status");
    expect(findNavItem("/attendance/2026-11-11")?.label).toBe("Attendance");
    expect(findNavItem("/nowhere")).toBeUndefined();
  });
});
