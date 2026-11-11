/**
 * Theme primitives for the Phase 4 dashboard.
 *
 * Deliberately framework-free so every rule is unit-testable without a DOM:
 * the React hook and the inline bootstrap script both consume these pure
 * functions (or the constant they are derived from).
 *
 * Precedence (single source of truth): an explicit stored preference wins;
 * otherwise the operating system preference decides.
 */

export type Theme = "light" | "dark";

/** What the user chose: an explicit theme, or "follow the system". */
export type ThemePreference = Theme | "system";

export const THEME_STORAGE_KEY = "attendance-dashboard-theme";

export const THEME_ATTRIBUTE = "data-theme";

export function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

/** Parse a raw stored value; anything unrecognised means "follow the system". */
export function parseThemePreference(
  raw: string | null | undefined,
): ThemePreference {
  return isTheme(raw) ? raw : "system";
}

/** Resolve a preference into the theme that is actually applied. */
export function resolveTheme(
  preference: ThemePreference,
  systemPrefersDark: boolean,
): Theme {
  if (preference === "system") {
    return systemPrefersDark ? "dark" : "light";
  }
  return preference;
}

export function otherTheme(theme: Theme): Theme {
  return theme === "dark" ? "light" : "dark";
}

/** Apply a theme to the document root (attribute + native control colour). */
export function applyTheme(
  theme: Theme,
  root: HTMLElement = document.documentElement,
): void {
  root.setAttribute(THEME_ATTRIBUTE, theme);
  root.style.colorScheme = theme;
}

/** Read the stored preference; storage failures degrade to "system". */
export function readStoredPreference(
  storage: Pick<Storage, "getItem"> | null | undefined,
): ThemePreference {
  if (!storage) {
    return "system";
  }
  try {
    return parseThemePreference(storage.getItem(THEME_STORAGE_KEY));
  } catch {
    return "system";
  }
}

/** Persist an explicit preference; storage failures are never fatal. */
export function writeStoredPreference(
  storage: Pick<Storage, "setItem"> | null | undefined,
  preference: ThemePreference,
): void {
  if (!storage) {
    return;
  }
  try {
    storage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    /* Private-mode or quota failures must not break the UI. */
  }
}

/** Whether the OS currently asks for the dark scheme. */
export function systemPrefersDark(target: Window = window): boolean {
  if (typeof target.matchMedia !== "function") {
    return false;
  }
  try {
    return target.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

/**
 * Pre-paint bootstrap script.
 *
 * Runs synchronously in `<head>` **before** first paint, so the correct theme is
 * applied without any flash, and React hydrates against markup that already
 * matches the DOM. It never throws into the page: a storage or `matchMedia`
 * failure simply leaves the CSS default (dark) in place.
 */
export const THEME_INIT_SCRIPT = `(function(){try{var k='${THEME_STORAGE_KEY}';var s=window.localStorage.getItem(k);var prefersDark=typeof window.matchMedia==='function'?window.matchMedia('(prefers-color-scheme: dark)').matches:false;var theme=(s==='light'||s==='dark')?s:(prefersDark?'dark':'light');var root=document.documentElement;root.setAttribute('${THEME_ATTRIBUTE}',theme);root.style.colorScheme=theme;}catch(e){}})();`;
