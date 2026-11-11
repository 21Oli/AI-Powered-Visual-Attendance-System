import styles from "./page.module.css";

/**
 * P4.2 landing placeholder.
 *
 * Intentionally not a dashboard screen and not a navigation shell: Overview,
 * Employees, Attendance and System Status belong to P4.4–P4.7, and the sidebar
 * shell to P4.3. This page proves the scaffold, tokens and theme system work.
 */
export default function HomePage() {
  return (
    <section className={styles.hero} aria-labelledby="page-title">
      <p className={styles.eyebrow}>Phase 4 · P4.2</p>
      <h1 id="page-title" className={styles.title}>
        Dashboard scaffold ready
      </h1>
      <p className={styles.lead}>
        The Next.js App Router shell, the design tokens and the light/dark theme
        system are in place. Navigation, the four dashboard screens and the
        same-origin backend proxy arrive in later milestones.
      </p>
      <dl className={styles.facts}>
        <div className={styles.fact}>
          <dt>Framework</dt>
          <dd>Next.js 16 · App Router</dd>
        </div>
        <div className={styles.fact}>
          <dt>Language</dt>
          <dd>TypeScript 5.9 · strict</dd>
        </div>
        <div className={styles.fact}>
          <dt>Styling</dt>
          <dd>CSS Modules · custom properties</dd>
        </div>
      </dl>
      <p className={styles.note}>
        No attendance or employee data is displayed here yet, and no fixture
        data exists in this milestone.
      </p>
    </section>
  );
}
