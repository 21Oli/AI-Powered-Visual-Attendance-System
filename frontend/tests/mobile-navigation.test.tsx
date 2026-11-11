import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { pathRef } = vi.hoisted(() => ({ pathRef: { current: "/overview" } }));

vi.mock("next/navigation", () => ({
  usePathname: () => pathRef.current,
}));

import { AppShell } from "../src/components/layout/AppShell";
import { MOBILE_NAVIGATION_ID } from "../src/components/layout/MobileNavigation";

const renderShell = () =>
  render(
    <AppShell>
      <h1>Section content</h1>
    </AppShell>,
  );

const trigger = () => screen.getByRole("button", { name: "Open navigation" });
const drawer = () => screen.queryByRole("dialog", { name: "Navigation" });

describe("mobile navigation", () => {
  beforeEach(() => {
    pathRef.current = "/overview";
  });

  it("starts collapsed, with the drawer absent from the accessibility tree", () => {
    renderShell();

    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(trigger().getAttribute("aria-controls")).toBe(MOBILE_NAVIGATION_ID);
    expect(drawer()).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Mobile" })).toBeNull();
  });

  it("opens a labelled drawer containing all four areas and moves focus inside", async () => {
    const user = userEvent.setup();
    renderShell();

    await user.click(trigger());

    const panel = drawer();
    expect(panel).not.toBeNull();
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
    expect(panel?.getAttribute("id")).toBe(MOBILE_NAVIGATION_ID);
    expect(panel?.getAttribute("aria-modal")).toBe("true");

    const links = within(panel as HTMLElement).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/overview",
      "/employees",
      "/attendance",
      "/system-status",
    ]);

    const closeButton = screen.getByRole("button", { name: "Close navigation" });
    expect(document.activeElement).toBe(closeButton);
  });

  it("reflects the active route inside the drawer", async () => {
    const user = userEvent.setup();
    pathRef.current = "/employees";
    renderShell();

    await user.click(trigger());

    const mobileNav = screen.getByRole("navigation", { name: "Mobile" });
    const current = within(mobileNav)
      .getAllByRole("link")
      .filter((link) => link.getAttribute("aria-current") === "page");
    expect(current).toHaveLength(1);
    expect(current[0]?.textContent).toBe("Employees");
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    const user = userEvent.setup();
    renderShell();

    await user.click(trigger());
    await user.keyboard("{Escape}");

    expect(drawer()).toBeNull();
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(trigger());
  });

  it("closes when the close button is used", async () => {
    const user = userEvent.setup();
    renderShell();

    await user.click(trigger());
    await user.click(screen.getByRole("button", { name: "Close navigation" }));

    expect(drawer()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("closes after a navigation link is chosen", async () => {
    const user = userEvent.setup();
    renderShell();

    await user.click(trigger());
    const panel = drawer();
    await user.click(within(panel as HTMLElement).getByRole("link", { name: /employees/i }));

    expect(drawer()).toBeNull();
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
  });

  it("closes when the scrim is dismissed", async () => {
    const user = userEvent.setup();
    renderShell();

    await user.click(trigger());
    await user.click(screen.getByRole("button", { name: "Dismiss navigation" }));

    expect(drawer()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("can be operated by keyboard alone", async () => {
    const user = userEvent.setup();
    renderShell();

    // Document order: skip link, then the sidebar's links, then the menu
    // trigger in the header. jsdom does not apply the sidebar's media query, so
    // this is the desktop tab order.
    await user.tab();
    expect(document.activeElement?.textContent).toContain(
      "Skip to main content",
    );

    for (const label of [
      "Overview",
      "Employees",
      "Attendance",
      "System Status",
    ]) {
      await user.tab();
      expect(document.activeElement?.textContent).toBe(label);
    }

    await user.tab();
    expect(document.activeElement).toBe(trigger());

    await user.keyboard("{Enter}");
    expect(drawer()).not.toBeNull();

    await user.keyboard("{Escape}");
    expect(drawer()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });
});
