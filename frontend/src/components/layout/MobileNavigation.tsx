import Link from "next/link";
import type { RefObject } from "react";

import { NAV_ITEMS, isActivePath } from "../../lib/navigation";
import { NavIcon } from "../icons/NavIcon";
import styles from "./MobileNavigation.module.css";

export interface MobileNavigationProps {
  open: boolean;
  pathname: string | null;
  /** Explicit close (close button, scrim, Escape): returns focus to the trigger. */
  onClose: () => void;
  /** Close after following a link: the new page owns focus. */
  onNavigate: () => void;
  closeButtonRef: RefObject<HTMLButtonElement | null>;
}

/** Element id referenced by the header trigger's `aria-controls`. */
export const MOBILE_NAVIGATION_ID = "app-mobile-navigation";

/**
 * Narrow-screen navigation drawer.
 *
 * A modal dialog with a scrim: focus moves inside on open, Escape closes it, and
 * every link closes the drawer before navigating. It is only rendered while open,
 * so closed links are never reachable by keyboard or assistive technology.
 */
export function MobileNavigation({
  open,
  pathname,
  onClose,
  onNavigate,
  closeButtonRef,
}: MobileNavigationProps) {
  if (!open) {
    return null;
  }

  return (
    <div className={styles.layer}>
      <button
        type="button"
        className={styles.scrim}
        onClick={onClose}
        aria-label="Dismiss navigation"
        tabIndex={-1}
      />

      <div
        id={MOBILE_NAVIGATION_ID}
        className={styles.drawer}
        role="dialog"
        aria-modal="true"
        aria-label="Navigation"
      >
        <div className={styles.head}>
          <span className={styles.title}>Navigation</span>
          <button
            ref={closeButtonRef}
            type="button"
            className={styles.close}
            onClick={onClose}
            aria-label="Close navigation"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>

        <nav className={styles.nav} aria-label="Mobile">
          <ul className={styles.list}>
            {NAV_ITEMS.map((item) => {
              const active = isActivePath(pathname, item.href);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onNavigate}
                    className={
                      active ? `${styles.link} ${styles.active}` : styles.link
                    }
                    aria-current={active ? "page" : undefined}
                  >
                    <NavIcon name={item.icon} className={styles.icon} />
                    <span>{item.label}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
      </div>
    </div>
  );
}
