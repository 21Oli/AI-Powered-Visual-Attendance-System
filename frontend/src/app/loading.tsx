import styles from "./loading.module.css";

/** Route-level loading state: announced to assistive technology, no fake data. */
export default function Loading() {
  return (
    <div className={styles.wrapper} role="status" aria-live="polite">
      <span className={styles.srOnly}>Loading…</span>
      <div className={styles.bar} aria-hidden="true" />
      <div className={styles.barShort} aria-hidden="true" />
      <div className={styles.bar} aria-hidden="true" />
    </div>
  );
}
