import styles from "./MetricCard.module.css";

export type MetricCardState = "loading" | "ready" | "unavailable";

export interface MetricCardProps {
  label: string;
  /** Formatted value; ignored while loading or unavailable. */
  value: string | null;
  detail?: string;
  state: MetricCardState;
}

/**
 * One Overview figure.
 *
 * States are distinguishable by text, not colour alone: loading, a real value
 * (including `0`), or "Unavailable" when its section failed. A failed section
 * never renders a plausible number.
 */
export function MetricCard({ label, value, detail, state }: MetricCardProps) {
  const shown =
    state === "loading" ? "Loading…" : state === "unavailable" ? "Unavailable" : (value ?? "—");

  return (
    <li className={styles.card}>
      <p className={styles.label}>{label}</p>
      <p className={state === "ready" ? styles.value : styles.valueMuted}>
        {shown}
      </p>
      {detail === undefined ? null : <p className={styles.detail}>{detail}</p>}
    </li>
  );
}
