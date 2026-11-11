"use client";

import { useCallback, useEffect, useSyncExternalStore } from "react";

import type { Theme } from "../lib/theme";
import {
  ensureThemeApplied,
  getServerSnapshot,
  getSnapshot,
  setTheme as commitTheme,
  subscribe,
  toggleTheme as commitToggle,
} from "../lib/themeStore";

export interface ThemeController {
  /**
   * The applied theme, or `null` until the client snapshot is available.
   *
   * `null` is deliberate: the server cannot know the preference, so the first
   * render matches the server exactly. `getServerSnapshot` covers hydration and
   * React then re-reads the DOM, which the inline bootstrap script has already
   * set before first paint — no flash, no mismatch.
   */
  theme: Theme | null;
  ready: boolean;
  toggle: () => void;
  setTheme: (theme: Theme) => void;
}

/**
 * Subscribes the component tree to the theme that is applied to `<html>`.
 *
 * State lives in the DOM via the external store (`src/lib/themeStore.ts`), not
 * in component state, so there is no effect-driven `setState` cascade.
 */
export function useTheme(): ThemeController {
  const theme = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  // Synchronise once on mount in case the page was rendered without the
  // bootstrap script (for example a component rendered in isolation in tests).
  useEffect(() => {
    ensureThemeApplied();
  }, []);

  const setTheme = useCallback((next: Theme) => {
    commitTheme(next);
  }, []);

  const toggle = useCallback(() => {
    commitToggle();
  }, []);

  return { theme, ready: theme !== null, toggle, setTheme };
}
