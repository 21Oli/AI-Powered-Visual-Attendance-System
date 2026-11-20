/**
 * One-way notification that a protected request was refused with `401`.
 *
 * The API client is plain TypeScript and the auth state lives in React, so the
 * two are joined here instead of importing React into the transport. A data
 * request that the backend rejects as unauthenticated means the session this
 * page was using is gone; the provider clears the signed-in UI and the route
 * guard redirects to the login page with a safe return path.
 *
 * Deliberately tiny: no request identity, no error detail, no state. Only the
 * fact "the session was rejected" crosses the boundary, so nothing sensitive
 * can leak into a listener and a late notification cannot invent a user.
 */

type UnauthorizedListener = () => void;

const listeners = new Set<UnauthorizedListener>();

/** Subscribe to session rejections; returns the unsubscribe function. */
export function subscribeToUnauthorized(
  listener: UnauthorizedListener,
): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Announce that a protected request was answered with `401`. */
export function notifyUnauthorized(): void {
  for (const listener of [...listeners]) {
    listener();
  }
}
