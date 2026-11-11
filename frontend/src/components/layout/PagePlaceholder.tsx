import styles from "./PagePlaceholder.module.css";

export interface PagePlaceholderProps {
  title: string;
  purpose: string;
  planned: readonly string[];
}

/**
 * Structural placeholder for a dashboard route.
 *
 * Shows the page title, what the area is for, and what is planned — never sample
 * employees, attendance events, metrics or health results. Those arrive with the
 * real data wiring in later milestones.
 */
export function PagePlaceholder({
  title,
  purpose,
  planned,
}: PagePlaceholderProps) {
  return (
    <section className={styles.page} aria-labelledby="page-title">
      <div className={styles.heading}>
        <h1 id="page-title" className={styles.title}>
          {title}
        </h1>
        <span className={styles.badge}>Under development</span>
      </div>

      <p className={styles.purpose}>{purpose}</p>

      <div className={styles.panel}>
        <h2 className={styles.panelTitle}>Planned for this area</h2>
        <ul className={styles.list}>
          {planned.map((entry) => (
            <li key={entry} className={styles.item}>
              {entry}
            </li>
          ))}
        </ul>
      </div>

      <p className={styles.note}>
        Structural placeholder only — no employee, attendance or health data is
        displayed yet.
      </p>
    </section>
  );
}
