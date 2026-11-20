import Link from "next/link";

import { sessionErrorMessage } from "../../lib/auth/messages";
import { ACCESS_DENIED_MESSAGE } from "../../lib/api/errors";
import { DEFAULT_ROUTE } from "../../lib/navigation";
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
/**
 * Shown when the session is valid but the role may not view this area.
 *
 * Distinct from a session failure on purpose: the visitor is told the session
 * is fine, nothing was signed out, and the only action is going back to an
 * area the role can read. No retry is offered, because a forbidden request is
 * not fixed by repeating it.
 */
export function AccessDenied({
  message = ACCESS_DENIED_MESSAGE,
}: {
  message?: string;
}) {
  return (
    <main className={styles.page}>
      <section className={styles.panel} aria-labelledby="access-denied-title">
        <h1 id="access-denied-title" className={styles.title}>
          Access denied
        </h1>
        <p className={styles.text} role="alert">
          {message}
        </p>
        <p className={styles.note}>
          Your session is still active. Your role does not include this area or
          action.
        </p>
        <Link className={styles.link} href={DEFAULT_ROUTE}>
          Back to overview
        </Link>
      </section>
    </main>
  );
}

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
