"use client";

import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { NAV_ITEMS, findNavItem } from "../../lib/navigation";
import { Header } from "./Header";
import { MobileNavigation } from "./MobileNavigation";
import { Sidebar } from "./Sidebar";
import styles from "./AppShell.module.css";

/**
 * Shared dashboard shell: skip link, desktop sidebar, header, mobile drawer and
 * the main content region that every dashboard route renders into.
 *
 * Only the drawer's open state is client state. Focus handling is done with refs
 * and DOM calls (never `setState` inside an effect): opening moves focus to the
 * close button, closing returns it to the trigger.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);

  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusRef = useRef(false);

  const openMenu = useCallback(() => {
    restoreFocusRef.current = false;
    setMenuOpen(true);
  }, []);

  const closeMenu = useCallback(() => {
    restoreFocusRef.current = true;
    setMenuOpen(false);
  }, []);

  const closeAfterNavigation = useCallback(() => {
    // The destination page owns focus after a navigation, so do not pull it
    // back to the trigger button.
    restoreFocusRef.current = false;
    setMenuOpen(false);
  }, []);

  useEffect(() => {
    if (menuOpen) {
      closeButtonRef.current?.focus({ preventScroll: true });
      return;
    }
    if (restoreFocusRef.current) {
      restoreFocusRef.current = false;
      menuButtonRef.current?.focus({ preventScroll: true });
    }
  }, [menuOpen]);

  useEffect(() => {
    if (!menuOpen) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") {
        return;
      }
      restoreFocusRef.current = true;
      setMenuOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [menuOpen]);

  const sectionLabel = findNavItem(pathname)?.label ?? NAV_ITEMS[0].label;

  return (
    <div className={styles.shell}>
      <a className={styles.skipLink} href="#main-content">
        Skip to main content
      </a>

      <Sidebar pathname={pathname} />

      <div className={styles.column}>
        <Header
          sectionLabel={sectionLabel}
          menuOpen={menuOpen}
          onOpenMenu={openMenu}
          menuButtonRef={menuButtonRef}
        />

        <MobileNavigation
          open={menuOpen}
          pathname={pathname}
          onClose={closeMenu}
          onNavigate={closeAfterNavigation}
          closeButtonRef={closeButtonRef}
        />

        <main id="main-content" tabIndex={-1} className={styles.main}>
          <div className={styles.content}>{children}</div>
        </main>
      </div>
    </div>
  );
}
