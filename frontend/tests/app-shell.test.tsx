import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { pathRef } = vi.hoisted(() => ({ pathRef: { current: "/overview" } }));

vi.mock("next/navigation", () => ({
  usePathname: () => pathRef.current,
}));

import { AppShell } from "../src/components/layout/AppShell";

const renderShell = () =>
  render(
    <AppShell>
      <h1>Section content</h1>
    </AppShell>,
  );

const primaryNav = () => screen.getByRole("navigation", { name: "Primary" });

describe("AppShell", () => {
  beforeEach(() => {
    pathRef.current = "/overview";
  });

  it("provides skip link, banner, primary navigation and main content", () => {
    renderShell();

    expect(
      screen
        .getByRole("link", { name: /skip to main content/i })
        .getAttribute("href"),
    ).toBe("#main-content");
    expect(screen.getByRole("banner")).toBeDefined();
    expect(screen.getByRole("main")).toBeDefined();
    expect(primaryNav()).toBeDefined();
    expect(
      screen.getByRole("heading", { level: 1, name: /section content/i }),
    ).toBeDefined();
  });

  it("links to all four dashboard areas", () => {
    renderShell();

    const links = within(primaryNav()).getAllByRole("link");
    expect(links).toHaveLength(4);
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/overview",
      "/employees",
      "/attendance",
      "/system-status",
    ]);
    expect(links.map((link) => link.textContent)).toEqual([
      "Overview",
      "Employees",
      "Attendance",
      "System Status",
    ]);
  });

  it("marks only the current route as the active page", () => {
    pathRef.current = "/attendance";
    renderShell();

    const current = within(primaryNav())
      .getAllByRole("link")
      .filter((link) => link.getAttribute("aria-current") === "page");

    expect(current).toHaveLength(1);
    expect(current[0]?.textContent).toBe("Attendance");
  });

  it("shows the current area in the header without duplicating the heading", () => {
    pathRef.current = "/system-status";
    renderShell();

    expect(screen.getByRole("banner").textContent).toContain("System Status");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("falls back to the default area on an unknown route", () => {
    pathRef.current = "/no-such-page";
    renderShell();

    expect(screen.getByRole("banner").textContent).toContain("Overview");
    expect(
      within(primaryNav())
        .getAllByRole("link")
        .some((link) => link.getAttribute("aria-current") === "page"),
    ).toBe(false);
  });

  it("keeps the theme control available inside the shell", async () => {
    const user = userEvent.setup();
    renderShell();

    const toggle = screen.getByRole("button", { name: /theme/i });
    expect(toggle).toBeDefined();
    await user.click(toggle);

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("works in the light theme as well", async () => {
    window.localStorage.setItem("attendance-dashboard-theme", "light");
    const user = userEvent.setup();
    renderShell();

    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(within(primaryNav()).getAllByRole("link")).toHaveLength(4);

    await user.click(screen.getByRole("button", { name: /theme/i }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});
