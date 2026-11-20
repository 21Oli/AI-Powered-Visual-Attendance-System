"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { useOptionalAuth } from "../../lib/auth/AuthProvider";
import type { AuthContextValue } from "../../lib/auth/AuthProvider";
import { SIGN_OUT_FAILED_MESSAGE } from "../../lib/auth/messages";
import { LOGIN_PATH } from "../../lib/auth/paths";
import { Button } from "../ui/Button";
import styles from "./UserMenu.module.css";

/**
 * Identity plus sign-out for the shell header.
 *
 * Rendered only for an authenticated session. It is optional-auth on purpose:
 * the shell is also rendered in isolation (component tests), where there is no
 * provider and therefore no session UI — never a second auth decision.
 *
 * Sign-out calls the existing `POST /api/v1/auth/logout`, which revokes the
 * session server-side and clears the cookie. The client state is cleared only
 * after that succeeds; on failure the user stays signed in and is told the
 * sign-out did not complete, so a stale "signed out" screen cannot be shown
 * while the cookie is still live.
 */
export function UserMenu() {
  const auth = useOptionalAuth();
  if (auth === null || auth.status !== "authenticated" || auth.user === null) {
    return null;
  }
  // The router hook lives in the signed-in child so the shell can also render
  // without a provider or a router (it does, in component tests).
  return <SignedInMenu auth={auth} />;
}

function SignedInMenu({ auth }: { auth: AuthContextValue }) {
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const user = auth.user;
  if (user === null) {
    return null;
  }

  async function handleSignOut() {
    if (signingOut) {
      return;
    }
    setSigningOut(true);
    setError(null);
    try {
      await auth.signOut();
      router.replace(LOGIN_PATH);
    } catch {
      setError(SIGN_OUT_FAILED_MESSAGE);
      setSigningOut(false);
    }
  }

  return (
    <div className={styles.menu}>
      <span className={styles.identity}>
        <span className={styles.username}>{user.username}</span>
        <span className={styles.role}>{user.role}</span>
      </span>
      <Button
        size="sm"
        variant="secondary"
        onClick={handleSignOut}
        disabled={signingOut}
        aria-busy={signingOut}
      >
        {signingOut ? "Signing out…" : "Sign out"}
      </Button>
      {error === null ? null : (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
