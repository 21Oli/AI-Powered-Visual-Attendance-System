import Link from "next/link";

import { NAV_ITEMS, isActivePath } from "../../lib/navigation";
import { NavIcon } from "../icons/NavIcon";
import styles from "./Sidebar.module.css";

/**
 * Persistent desktop navigation.
 *
 * Hidden below 900px by CSS; the mobile drawer (`MobileNavigation`) owns the
 * narrow-screen experience. The current route is exposed with
 * `aria-current="page"`, not colour alone.
 */
export function Sidebar({ pathname }: { pathname: string | null }) {
  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <span className={styles.mark} aria-hidden="true" />
        <span className={styles.brandText}>
          <span className={styles.brandName}>Visual Attendance</span>
          <span className={styles.brandMeta}>Operator dashboard</span>
        </span>
      </div>

      <nav className={styles.nav} aria-label="Primary">
        <ul className={styles.list}>
          {NAV_ITEMS.map((item) => {
            const active = isActivePath(pathname, item.href);
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={active ? `${styles.link} ${styles.active}` : styles.link}
                  aria-current={active ? "page" : undefined}
                >
                  <NavIcon name={item.icon} className={styles.icon} />
                  <span className={styles.label}>{item.label}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      <p className={styles.footnote}>Phase 4 · structural scaffold</p>
    </aside>
  );
}
