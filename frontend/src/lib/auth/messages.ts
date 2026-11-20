/**
 * User-facing wording for authentication failures.
 *
 * The backend already returns sanitized envelopes, but the UI deliberately
 * maps them to its own fixed sentences: no backend `message`, status text,
 * driver detail, host, username or token is ever rendered or logged. The
 * mapping is exhaustive over `ApiError.kind`/`status` so a new failure mode
 * falls back to a generic sentence instead of leaking through.
 */

import { ApiError, toApiError } from "../api/errors";

export const LOGIN_INVALID_CREDENTIALS_MESSAGE =
  "The username or password is incorrect.";
export const LOGIN_RATE_LIMITED_MESSAGE =
  "Too many sign-in attempts. Wait a moment, then try again.";
export const LOGIN_ORIGIN_REFUSED_MESSAGE =
  "Sign-in is not allowed from this address.";
export const LOGIN_VALIDATION_MESSAGE = "Enter a valid username and password.";
export const LOGIN_NETWORK_MESSAGE =
  "The dashboard could not reach its API. Check your connection and try again.";
export const LOGIN_UNAVAILABLE_MESSAGE =
  "Sign-in is temporarily unavailable. Try again in a moment.";
export const LOGIN_MALFORMED_MESSAGE =
  "The service returned an unexpected response. Try again.";
export const LOGIN_GENERIC_MESSAGE = "Sign-in could not be completed. Try again.";

export const SESSION_EXPIRED_MESSAGE =
  "Your session has expired. Sign in to continue.";
export const SESSION_NETWORK_MESSAGE =
  "The dashboard could not reach its API, so your session could not be checked. Check your connection and try again.";
export const SESSION_UNAVAILABLE_MESSAGE =
  "The service is temporarily unavailable, so your session could not be checked. Try again in a moment.";
export const SESSION_MALFORMED_MESSAGE =
  "The dashboard received an unexpected response while checking your session. Try again.";
export const SESSION_GENERIC_MESSAGE =
  "Your session could not be verified. Try again.";

export const SIGN_OUT_FAILED_MESSAGE =
  "Sign-out could not be completed. Try again.";

function isUnavailableStatus(error: ApiError): boolean {
  return (
    error.status === 502 || error.status === 503 || error.status === 504
  );
}

/** Sanitized message for a failed sign-in attempt. */
export function loginErrorMessage(cause: unknown): string {
  const error = toApiError(cause);
  if (error.kind === "network") {
    return LOGIN_NETWORK_MESSAGE;
  }
  if (error.kind === "http") {
    if (error.status === 401) {
      return LOGIN_INVALID_CREDENTIALS_MESSAGE;
    }
    if (error.status === 429) {
      return LOGIN_RATE_LIMITED_MESSAGE;
    }
    if (error.status === 403) {
      return LOGIN_ORIGIN_REFUSED_MESSAGE;
    }
    if (error.status === 422) {
      return LOGIN_VALIDATION_MESSAGE;
    }
    if (isUnavailableStatus(error)) {
      return LOGIN_UNAVAILABLE_MESSAGE;
    }
  }
  if (error.kind === "malformed") {
    return LOGIN_MALFORMED_MESSAGE;
  }
  return LOGIN_GENERIC_MESSAGE;
}

/**
 * Sanitized message for a session that could not be *verified*.
 *
 * A network or 5xx failure is never treated as "signed out": the message says
 * the check failed, and the caller keeps the user on a retry screen.
 */
export function sessionErrorMessage(cause: unknown): string {
  const error = toApiError(cause);
  if (error.kind === "network") {
    return SESSION_NETWORK_MESSAGE;
  }
  if (error.kind === "http" && isUnavailableStatus(error)) {
    return SESSION_UNAVAILABLE_MESSAGE;
  }
  if (error.kind === "malformed") {
    return SESSION_MALFORMED_MESSAGE;
  }
  return SESSION_GENERIC_MESSAGE;
}
