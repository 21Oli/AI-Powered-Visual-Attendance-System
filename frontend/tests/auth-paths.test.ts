import { describe, expect, it } from "vitest";

import { loginPathFor, safeReturnPath } from "../src/lib/auth/paths";
import { DEFAULT_ROUTE } from "../src/lib/navigation";

describe("safeReturnPath", () => {
  it("keeps an approved dashboard path, with its query string", () => {
    expect(safeReturnPath("/employees")).toBe("/employees");
    expect(safeReturnPath("/attendance?after=EMP-9&limit=50")).toBe(
      "/attendance?after=EMP-9&limit=50",
    );
    expect(safeReturnPath("/system-status")).toBe("/system-status");
  });

  it("drops a fragment but keeps the accepted path", () => {
    expect(safeReturnPath("/overview#top")).toBe("/overview");
  });

  it("falls back to the default route for anything unsuitable", () => {
    const rejected = [
      null,
      undefined,
      "",
      "   ",
      "employees",
      "/unknown",
      "/login",
      "/login?next=/overview",
      "/api/v1/auth/me",
      "/api/backend/api/v1/health",
      "//evil.test/overview",
      "///evil.test",
      "https://evil.test/overview",
      "http://evil.test/overview",
      "javascript:alert(1)",
      "/\\evil.test",
      "/employees/../login",
      "/employees/./overview",
      "/employees/%2e%2e/login",
      "/overview\n",
      `/overview${"x".repeat(600)}`,
    ];

    for (const value of rejected) {
      expect(safeReturnPath(value)).toBe(DEFAULT_ROUTE);
    }
  });

  it("accepts a caller-supplied fallback", () => {
    expect(safeReturnPath("https://evil.test", "/attendance")).toBe(
      "/attendance",
    );
  });

  it("never returns a protocol-relative or absolute URL", () => {
    for (const value of ["//evil.test", "https://evil.test", "/\\evil.test"]) {
      const result = safeReturnPath(value);
      expect(result.startsWith("/")).toBe(true);
      expect(result.startsWith("//")).toBe(false);
      expect(result).not.toContain("evil.test");
    }
  });
});

describe("loginPathFor", () => {
  it("encodes an approved return path into the login URL", () => {
    expect(loginPathFor("/employees")).toBe("/login?next=%2Femployees");
    expect(loginPathFor("/attendance?limit=10")).toBe(
      "/login?next=%2Fattendance%3Flimit%3D10",
    );
  });

  it("never forwards an unsafe value", () => {
    expect(loginPathFor("https://evil.test/overview")).toBe(
      `/login?next=${encodeURIComponent(DEFAULT_ROUTE)}`,
    );
    expect(loginPathFor("/login")).toBe(
      `/login?next=${encodeURIComponent(DEFAULT_ROUTE)}`,
    );
  });
});
