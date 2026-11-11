import type { RefObject } from "react";

import { ThemeToggle } from "../ui/ThemeToggle";
import styles from "./Header.module.css";

export interface HeaderProps {
  /** Label of the dashboard area the current route belongs to. */
  sectionLabel: string;
  menuOpen: boolean;
  onOpenMenu: () => void;
  menuButtonRef: RefObject<HTMLButtonElement | null>;
}

/**
 * Top bar of the shell.
 *
 * Owns the mobile menu trigger (`aria-expanded` + `aria-controls`), the current
 * section label and the existing theme toggle. The page itself renders the
 * `<h1>`, so the label here is deliberately not a heading.
 */
export function Header({
  sectionLabel,
  menuOpen,
  onOpenMenu,
  menuButtonRef,
}: HeaderProps) {
  return (
    <header className={styles.header}>
      <button
        ref={menuButtonRef}
        type="button"
        className={styles.menuButton}
        onClick={onOpenMenu}
        aria-label="Open navigation"
        aria-expanded={menuOpen}
        aria-controls="app-mobile-navigation"
      >
        <span className={styles.bars} aria-hidden="true">
          <span className={styles.bar} />
          <span className={styles.bar} />
          <span className={styles.bar} />
        </span>
      </button>

      <p className={styles.section}>{sectionLabel}</p>

      <div className={styles.actions}>
        <ThemeToggle />
      </div>
    </header>
  );
}
