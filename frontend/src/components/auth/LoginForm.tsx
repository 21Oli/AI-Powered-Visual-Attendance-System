"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useAuth } from "../../lib/auth/AuthProvider";
import {
  SESSION_EXPIRED_MESSAGE,
  loginErrorMessage,
} from "../../lib/auth/messages";
import { safeReturnPath } from "../../lib/auth/paths";
import { Button } from "../ui/Button";
import { StatusMessage } from "../ui/StatusMessage";
import { TextField } from "../ui/TextField";
import { AuthPending } from "./AuthScreens";
import styles from "./LoginForm.module.css";

/**
 * The dashboard sign-in form.
 *
 * The credential pair is posted to the existing `POST /api/v1/auth/login`; the
 * `Set-Cookie` response establishes the HttpOnly session and the token never
 * enters this component, React state, or any browser storage.
 *
 * A user who is already authenticated never sees the form: the page shows the
 * pending state and replaces the route with the (validated) return path.
 */
export function LoginForm({ nextPath }: { nextPath: string | null }) {
  const auth = useAuth();
  const router = useRouter();

  const destination = safeReturnPath(nextPath);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<{
    username?: string;
    password?: string;
  }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (auth.status === "authenticated") {
      router.replace(destination);
    }
  }, [auth.status, destination, router]);

  if (auth.status === "loading" || auth.status === "authenticated") {
    return <AuthPending label="Checking your session…" />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    // One submission at a time: the guard covers a double click, a held Enter
    // key and a second submit fired before the button re-renders as disabled.
    if (submitting) {
      return;
    }

    const errors: { username?: string; password?: string } = {};
    if (username.trim().length === 0) {
      errors.username = "Enter your username.";
    }
    if (password.length === 0) {
      errors.password = "Enter your password.";
    }
    setFieldErrors(errors);
    if (errors.username !== undefined || errors.password !== undefined) {
      setFormError(null);
      return;
    }

    setFormError(null);
    setSubmitting(true);
    try {
      await auth.signIn(username.trim(), password);
      // Verified locally: only an approved dashboard path can be targeted.
      router.replace(destination);
      // `submitting` stays true: the redirect is in progress and a second
      // credential exchange must not be possible.
    } catch (cause) {
      setFormError(loginErrorMessage(cause));
      setSubmitting(false);
    }
  }

  return (
    <main className={styles.page}>
      <section className={styles.card} aria-labelledby="login-title">
        <p className={styles.eyebrow}>Visual Attendance</p>
        <h1 id="login-title" className={styles.title}>
          Sign in
        </h1>
        <p className={styles.subtitle}>
          Use your operator account to open the dashboard. Roles and permissions
          are enforced by the API, not by this page.
        </p>

        {auth.notice === "expired" && formError === null ? (
          <StatusMessage tone="info">{SESSION_EXPIRED_MESSAGE}</StatusMessage>
        ) : null}

        <form className={styles.form} onSubmit={handleSubmit} noValidate>
          <TextField
            id="username"
            label="Username"
            value={username}
            onChange={(value) => {
              setUsername(value);
              if (fieldErrors.username !== undefined) {
                setFieldErrors((previous) => ({
                  ...previous,
                  username: undefined,
                }));
              }
            }}
            error={fieldErrors.username ?? null}
            required
            autoComplete="username"
            maxLength={128}
            disabled={submitting}
          />

          <TextField
            id="password"
            label="Password"
            type={showPassword ? "text" : "password"}
            value={password}
            onChange={(value) => {
              setPassword(value);
              if (fieldErrors.password !== undefined) {
                setFieldErrors((previous) => ({
                  ...previous,
                  password: undefined,
                }));
              }
            }}
            error={fieldErrors.password ?? null}
            required
            autoComplete="current-password"
            disabled={submitting}
            adornment={
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setShowPassword((current) => !current)}
                aria-pressed={showPassword}
                aria-label={showPassword ? "Hide password" : "Show password"}
                disabled={submitting}
              >
                {showPassword ? "Hide" : "Show"}
              </Button>
            }
          />

          {formError === null ? null : (
            <StatusMessage tone="error">{formError}</StatusMessage>
          )}

          <Button
            type="submit"
            variant="primary"
            className={styles.submit}
            disabled={submitting}
            aria-busy={submitting}
          >
            {submitting ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className={styles.footnote}>
          Access is limited to authorized staff. Sessions expire automatically.
        </p>
      </section>
    </main>
  );
}
