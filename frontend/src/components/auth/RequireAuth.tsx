"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import type { ReactNode } from "react";

import { useAuth } from "../../lib/auth/AuthProvider";
import { loginPathFor } from "../../lib/auth/paths";
import { AuthPending, AuthUnavailable } from "./AuthScreens";

/**
 * Protects the dashboard route group.
 *
 * Why client-side: the session cookie is issued with `Path=/api/v1`, so the
 * browser does not send it to `/overview` (or any other dashboard URL). A
 * Next.js middleware or server component therefore cannot see the session, and
 * inventing a second cookie or widening the path would break the approved
 * cookie policy. The guard asks the backend's `/api/v1/auth/me` instead, which
 * remains the single authority on whether a session is live.
 *
 * Guarantees:
 *
 * * children render only when `status === "authenticated"` — loading and
 *   redirecting states render a status screen, never the protected page;
 * * `unauthenticated` (401) redirects once to `/login?next=<safe path>`;
 * * `error` (network/503) does **not** redirect — it offers a retry, because an
 *   unreachable backend is not proof that the session ended;
 * * the redirect target is validated by `loginPathFor`, so no open redirect is
 *   possible and the login page can never be named as a target (no loop).
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (auth.status !== "unauthenticated") {
      return;
    }
    const search =
      typeof window === "undefined" ? "" : window.location.search;
    const pathname =
      typeof window === "undefined" ? "" : window.location.pathname;
    router.replace(loginPathFor(`${pathname}${search}`));
  }, [auth.status, router]);

  if (auth.status === "authenticated") {
    return <>{children}</>;
  }

  if (auth.status === "error") {
    return <AuthUnavailable error={auth.error} onRetry={auth.refresh} />;
  }

  return (
    <AuthPending
      label={
        auth.status === "unauthenticated"
          ? "Redirecting to sign-in…"
          : "Checking your session…"
      }
    />
  );
}
