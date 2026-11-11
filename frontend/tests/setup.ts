import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";

/**
 * Test environment setup.
 *
 * jsdom does not implement `window.matchMedia`, and it keeps `localStorage`
 * between tests in the same file, so both are normalised here. No test-only
 * package is introduced: `@testing-library/react`'s `cleanup` plus plain
 * `Object.defineProperty` are sufficient.
 */
const matchMediaStub = (query: string): MediaQueryList =>
  ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }) as unknown as MediaQueryList;

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: matchMediaStub,
  });
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.style.colorScheme = "";
});

afterEach(() => {
  cleanup();
});
