import { sessionErrorMessage } from "../../lib/auth/messages";
import { Button } from "../ui/Button";
import styles from "./AuthScreens.module.css";

/**
 * Shown while the session is being checked, and while a redirect is pending.
 *
 * It is deliberately *not* a spinner over the dashboard: protected content is
 * never mounted until `/me` has answered, so there is nothing to flash. The
 * live region announces the wait once.
 */
export function AuthPending({ label }: { label: string }) {
  return (
    <main className={styles.page} aria-busy="true">
      <p className={styles.status} role="status">
        {label}
      </p>
    </main>
  );
}

/**
 * Shown when the session could not be verified (network, 503, malformed).
 *
 * The user is explicitly told they have not been signed out, and the only
 * action is a user-initiated retry — no timer, so there is no retry loop.
 */
export function AuthUnavailable({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <main className={styles.page}>
      <section className={styles.panel} aria-labelledby="auth-unavailable-title">
        <h1 id="auth-unavailable-title" className={styles.title}>
          Session check failed
        </h1>
        <p className={styles.text} role="alert">
          {sessionErrorMessage(error)}
        </p>
        <p className={styles.note}>You have not been signed out.</p>
        <Button variant="primary" onClick={onRetry}>
          Try again
        </Button>
      </section>
    </main>
  );
}
