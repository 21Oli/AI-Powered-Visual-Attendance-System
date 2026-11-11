import Link from "next/link";

import { DEFAULT_ROUTE } from "../lib/navigation";
import styles from "./not-found.module.css";

export default function NotFound() {
  return (
    <main className={styles.panel}>
      <p className={styles.code}>404</p>
      <h1 className={styles.title}>Page not found</h1>
      <p className={styles.text}>
        That address is not part of the dashboard. Use the navigation, or start
        from the Overview area.
      </p>
      <Link className={styles.link} href={DEFAULT_ROUTE}>
        Go to Overview
      </Link>
    </main>
  );
}
