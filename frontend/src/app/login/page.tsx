import type { Metadata } from "next";

import { LoginForm } from "../../components/auth/LoginForm";

/**
 * The sign-in route (`/login`).
 *
 * It lives outside the `(dashboard)` group on purpose: it must not render the
 * authenticated shell, and the guard must not be able to redirect it to itself.
 * The return path is read on the server and validated again on the client by
 * `safeReturnPath`, so an `?next=` value is never trusted as a destination.
 */
export const metadata: Metadata = {
  title: "Sign in",
};

export const dynamic = "force-dynamic";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const raw = params.next;
  const next = Array.isArray(raw) ? raw[0] : raw;

  return <LoginForm nextPath={typeof next === "string" ? next : null} />;
}
