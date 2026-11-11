"use client";

import { Button } from "../components/ui/Button";
import styles from "./error.module.css";

/**
 * Route error boundary.
 *
 * Shows a fixed, sanitized message: the raw error message (which could carry a
 * server path or internal detail) is never rendered. Only Next's opaque `digest`
 * is shown, because it is designed for exactly this purpose.
 */
export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className={styles.panel} role="alert">
      <h1 className={styles.title}>Something went wrong</h1>
      <p className={styles.text}>
        This page could not be displayed. The failure was contained and no data
        was changed.
      </p>
      {error.digest ? (
        <p className={styles.digest}>
          Reference: <code>{error.digest}</code>
        </p>
      ) : null}
      <Button variant="primary" onClick={reset}>
        Try again
      </Button>
    </div>
  );
}
