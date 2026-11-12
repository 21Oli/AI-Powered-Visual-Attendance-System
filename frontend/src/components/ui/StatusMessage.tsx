import type { ReactNode } from "react";

import styles from "./StatusMessage.module.css";

export type StatusTone = "success" | "error" | "info";

/**
 * Inline outcome message.
 *
 * Errors use `role="alert"` (announced immediately); successes and neutral
 * notices use `role="status"` (announced politely). Success is only ever
 * rendered after the API call that produced it resolved.
 */
export function StatusMessage({
  tone,
  children,
  onDismiss,
}: {
  tone: StatusTone;
  children: ReactNode;
  onDismiss?: () => void;
}) {
  return (
    <div
      className={`${styles.message} ${styles[tone] ?? ""}`}
      role={tone === "error" ? "alert" : "status"}
    >
      <p className={styles.text}>{children}</p>
      {onDismiss === undefined ? null : (
        <button
          type="button"
          className={styles.dismiss}
          onClick={onDismiss}
          aria-label="Dismiss message"
        >
          <span aria-hidden="true">✕</span>
        </button>
      )}
    </div>
  );
}
