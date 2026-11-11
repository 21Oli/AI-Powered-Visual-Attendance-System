import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Guards the design system against silent drift.
 *
 * CSS Modules cannot be inspected through the DOM in jsdom, so the token sheet
 * itself is the subject: every required token must be declared in BOTH theme
 * blocks (the dark block is the default `:root`, the light block follows).
 */
const css = readFileSync(
  join(process.cwd(), "src", "app", "globals.css"),
  "utf8",
);

const lightBlockStart = css.indexOf('[data-theme="light"]');

const darkBlock = lightBlockStart === -1 ? "" : css.slice(0, lightBlockStart);
const lightBlock = lightBlockStart === -1 ? "" : css.slice(lightBlockStart);

const REQUIRED_TOKENS = [
  "--color-bg",
  "--color-bg-raised",
  "--color-surface",
  "--color-surface-hover",
  "--color-border",
  "--color-border-strong",
  "--color-text",
  "--color-text-muted",
  "--color-text-subtle",
  "--color-accent",
  "--color-accent-hover",
  "--color-accent-contrast",
  "--color-focus",
  "--color-success",
  "--color-warning",
  "--color-danger",
  "--shadow-sm",
  "--shadow-md",
  "--shadow-focus",
] as const;

const REQUIRED_SHARED_TOKENS = [
  "--space-1",
  "--space-4",
  "--space-8",
  "--radius-sm",
  "--radius-md",
  "--radius-lg",
  "--radius-pill",
  "--font-sans",
  "--font-mono",
  "--text-xs",
  "--text-base",
  "--text-3xl",
  "--leading-tight",
  "--weight-semibold",
  "--transition-fast",
] as const;

describe("design tokens", () => {
  it("has both theme blocks", () => {
    expect(lightBlockStart).toBeGreaterThan(0);
    expect(lightBlock.length).toBeGreaterThan(0);
  });

  it.each(REQUIRED_TOKENS)("defines %s in both themes", (token) => {
    expect(darkBlock).toContain(`${token}:`);
    expect(lightBlock).toContain(`${token}:`);
  });

  it.each(REQUIRED_SHARED_TOKENS)("defines shared token %s", (token) => {
    expect(css).toContain(`${token}:`);
  });

  it("provides a keyboard focus indicator", () => {
    expect(css).toContain(":focus-visible");
    expect(css).toContain("var(--color-focus)");
  });

  it("honours reduced-motion preferences", () => {
    expect(css).toContain("prefers-reduced-motion: reduce");
  });

  it("keeps dark as the default root theme", () => {
    // Dark tokens must be reachable without any attribute, i.e. applied to
    // `:root` as well as to `[data-theme="dark"]`.
    expect(darkBlock).toMatch(/:root,\s*\n\[data-theme="dark"\]/);
    expect(darkBlock).toContain("--color-bg");
    expect(lightBlock).not.toMatch(/:root\s*,/);
  });
});
