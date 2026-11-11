import {
  THEME_ATTRIBUTE,
  applyTheme,
  isTheme,
  otherTheme,
  readStoredPreference,
  resolveTheme,
  systemPrefersDark,
  writeStoredPreference,
} from "./theme";
import type { Theme } from "./theme";

/**
 * External theme store.
 *
 * The document (`data-theme` on `<html>`) is the source of truth; this module is
 * a thin, framework-free adapter so React can subscribe with
 * `useSyncExternalStore` instead of mirroring the DOM in local state. That avoids
 * both hydration mismatches and setState-inside-an-effect cascades.
 */

const listeners = new Set<() => void>();

let mediaQuery: MediaQueryList | null = null;
let mediaListener: ((event: MediaQueryListEvent) => void) | null = null;

function emit(): void {
  for (const listener of listeners) {
    listener();
  }
}

/** Current applied theme, or `null` when the document has none yet. */
export function getSnapshot(): Theme | null {
  if (typeof document === "undefined") {
    return null;
  }
  const value = document.documentElement.getAttribute(THEME_ATTRIBUTE);
  return isTheme(value) ? value : null;
}

/** The server cannot know the preference; the bootstrap script resolves it. */
export function getServerSnapshot(): Theme | null {
  return null;
}

function attachMediaQuery(): void {
  if (mediaQuery || typeof window === "undefined") {
    return;
  }
  if (typeof window.matchMedia !== "function") {
    return;
  }
  mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
  mediaListener = (event: MediaQueryListEvent): void => {
    // An explicit choice always wins; only "system" follows the OS.
    if (readStoredPreference(window.localStorage) !== "system") {
      return;
    }
    applyTheme(event.matches ? "dark" : "light");
    emit();
  };
  mediaQuery.addEventListener("change", mediaListener);
}

function detachMediaQuery(): void {
  if (!mediaQuery || !mediaListener) {
    return;
  }
  mediaQuery.removeEventListener("change", mediaListener);
  mediaQuery = null;
  mediaListener = null;
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    attachMediaQuery();
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      detachMediaQuery();
    }
  };
}

/**
 * Idempotent: apply the stored/system theme if the document does not already
 * carry the right one. Safe to call on every mount; the inline bootstrap script
 * normally makes this a no-op before first paint.
 */
export function ensureThemeApplied(): void {
  if (typeof document === "undefined" || typeof window === "undefined") {
    return;
  }
  const desired = resolveTheme(
    readStoredPreference(window.localStorage),
    systemPrefersDark(window),
  );
  if (getSnapshot() !== desired) {
    applyTheme(desired);
    emit();
  }
}

/** Apply and persist an explicit user choice. */
export function setTheme(theme: Theme): void {
  applyTheme(theme);
  writeStoredPreference(window.localStorage, theme);
  emit();
}

/** Flip to the opposite theme and persist it as an explicit choice. */
export function toggleTheme(): void {
  const current =
    getSnapshot() ??
    resolveTheme(
      readStoredPreference(window.localStorage),
      systemPrefersDark(window),
    );
  setTheme(otherTheme(current));
}
