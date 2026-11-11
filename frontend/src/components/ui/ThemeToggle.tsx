"use client";

import { useTheme } from "../../hooks/useTheme";
import styles from "./ThemeToggle.module.css";

/**
 * Accessible theme control.
 *
 * The visible label and `aria-label` are only specific once the theme is known,
 * so the first client render matches the server render exactly. The button is a
 * real `<button>` with a keyboard-visible focus ring from `globals.css`.
 */
export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const next = theme === "light" ? "dark" : "light";
  const label = theme ? `Switch to ${next} theme` : "Toggle colour theme";

  return (
    <button
      type="button"
      className={styles.toggle}
      onClick={toggle}
      aria-label={label}
      title={label}
    >
      <span className={styles.icon} aria-hidden="true">
        {theme === "light" ? "☀" : "☾"}
      </span>
      <span className={styles.text}>{theme === "light" ? "Light" : "Dark"}</span>
    </button>
  );
}
