import { describe, expect, it } from "vitest";

import {
  THEME_ATTRIBUTE,
  THEME_INIT_SCRIPT,
  THEME_STORAGE_KEY,
  applyTheme,
  isTheme,
  otherTheme,
  parseThemePreference,
  readStoredPreference,
  resolveTheme,
  systemPrefersDark,
  writeStoredPreference,
} from "../src/lib/theme";

const memoryStorage = (initial: Record<string, string> = {}) => {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key: string): string | null => data.get(key) ?? null,
    setItem: (key: string, value: string): void => {
      data.set(key, value);
    },
  };
};

describe("theme primitives", () => {
  it("recognises only the two supported themes", () => {
    expect(isTheme("dark")).toBe(true);
    expect(isTheme("light")).toBe(true);
    for (const value of ["system", "", null, undefined, "DARK", 1, {}]) {
      expect(isTheme(value)).toBe(false);
    }
  });

  it("treats any unknown stored value as 'system'", () => {
    expect(parseThemePreference("dark")).toBe("dark");
    expect(parseThemePreference("light")).toBe("light");
    expect(parseThemePreference("system")).toBe("system");
    expect(parseThemePreference("purple")).toBe("system");
    expect(parseThemePreference(null)).toBe("system");
    expect(parseThemePreference(undefined)).toBe("system");
  });

  it("resolves an explicit preference regardless of the OS", () => {
    expect(resolveTheme("dark", false)).toBe("dark");
    expect(resolveTheme("light", true)).toBe("light");
  });

  it("resolves 'system' from the OS preference", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
  });

  it("flips between the two themes", () => {
    expect(otherTheme("dark")).toBe("light");
    expect(otherTheme("light")).toBe("dark");
  });

  it("applies the theme as an attribute plus native colour scheme", () => {
    applyTheme("light");
    expect(document.documentElement.getAttribute(THEME_ATTRIBUTE)).toBe("light");
    expect(document.documentElement.style.colorScheme).toBe("light");

    applyTheme("dark");
    expect(document.documentElement.getAttribute(THEME_ATTRIBUTE)).toBe("dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
  });

  it("reads a stored preference and degrades to 'system'", () => {
    const storage = memoryStorage({ [THEME_STORAGE_KEY]: "dark" });
    expect(readStoredPreference(storage)).toBe("dark");
    expect(readStoredPreference(memoryStorage())).toBe("system");
    expect(readStoredPreference(null)).toBe("system");
    expect(readStoredPreference(undefined)).toBe("system");
    expect(
      readStoredPreference({
        getItem: () => {
          throw new Error("storage disabled");
        },
      }),
    ).toBe("system");
  });

  it("persists a preference and never throws on storage failure", () => {
    const storage = memoryStorage();
    writeStoredPreference(storage, "light");
    expect(storage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(() =>
      writeStoredPreference(
        {
          setItem: () => {
            throw new Error("quota exceeded");
          },
        },
        "dark",
      ),
    ).not.toThrow();
  });

  it("reports the OS preference defensively", () => {
    expect(systemPrefersDark(window)).toBe(false);
    expect(
      systemPrefersDark({
        matchMedia: () => ({ matches: true }),
      } as unknown as Window),
    ).toBe(true);
    expect(
      systemPrefersDark({
        matchMedia: () => {
          throw new Error("unavailable");
        },
      } as unknown as Window),
    ).toBe(false);
  });

  it("ships a bootstrap script keyed to the same storage key and attribute", () => {
    expect(THEME_INIT_SCRIPT).toContain(THEME_STORAGE_KEY);
    expect(THEME_INIT_SCRIPT).toContain(THEME_ATTRIBUTE);
    expect(THEME_INIT_SCRIPT).toContain("prefers-color-scheme: dark");
    expect(THEME_INIT_SCRIPT).toContain("localStorage");
    // The script must be self-contained and must not throw into the page.
    expect(THEME_INIT_SCRIPT).toContain("try{");
    expect(THEME_INIT_SCRIPT).toContain("catch(e)");
  });
});
