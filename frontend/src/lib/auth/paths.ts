/**
 * Safe redirect targets for the authentication flow.
 *
 * Every redirect in this feature is computed from this module, so a
 * client-supplied `next` value can never become an open redirect:
 *
 * * only a path rooted at `/` is accepted (no absolute or scheme URLs);
 * * protocol-relative (`//evil.test`) and backslash variants are refused;
 * * `.`/`..` segments are refused, so `/employees/../login` cannot be used to
 *   bounce a freshly signed-in user back to the sign-in page;
 * * control characters are refused;
 * * the pathname must belong to an approved dashboard area (`NAV_ITEMS`), so
 *   `/login`, backend paths and anything unknown fall back to the default.
 *
 * The query string of an accepted path is preserved (the employee directory
 * pages with `after`/`limit`), but the fragment is dropped.
 */

import { DEFAULT_ROUTE, findNavItem } from "../navigation";

/** Ceiling for an accepted return path; longer values are not dashboard URLs. */
const MAX_RETURN_PATH_LENGTH = 512;

/** Where a user who is not signed in is sent to authenticate. */
export const LOGIN_PATH = "/login";

function isSafePathname(pathname: string): boolean {
  // `//host` is protocol-relative and would leave the dashboard entirely.
  if (!pathname.startsWith("/") || pathname.startsWith("//")) {
    return false;
  }
  const segments = pathname.split("/").slice(1);
  for (const segment of segments) {
    // Refuse the traversal names in raw and percent-encoded form.
    if (segment === "." || segment === ".." || /^(%2e){1,2}$/i.test(segment)) {
      return false;
    }
  }
  return findNavItem(pathname) !== undefined;
}

/**
 * Return `value` when it is an approved internal dashboard path, otherwise
 * `fallback` (the default route by default).
 */
export function safeReturnPath(
  value: string | null | undefined,
  fallback: string = DEFAULT_ROUTE,
): string {
  if (typeof value !== "string") {
    return fallback;
  }
  const candidate = value.trim();
  if (candidate.length === 0 || candidate.length > MAX_RETURN_PATH_LENGTH) {
    return fallback;
  }
  if (/[\u0000-\u001f\u007f]/.test(candidate) || candidate.includes("\\")) {
    return fallback;
  }

  const hashIndex = candidate.indexOf("#");
  const withoutFragment =
    hashIndex === -1 ? candidate : candidate.slice(0, hashIndex);
  const queryIndex = withoutFragment.indexOf("?");
  const pathname =
    queryIndex === -1
      ? withoutFragment
      : withoutFragment.slice(0, queryIndex);
  const search =
    queryIndex === -1 ? "" : withoutFragment.slice(queryIndex);

  if (!isSafePathname(pathname)) {
    return fallback;
  }
  return `${pathname}${search}`;
}

/**
 * The sign-in URL for a protected route, carrying an already-validated return
 * path. An unusable target becomes the default route rather than an error.
 */
export function loginPathFor(target: string | null | undefined): string {
  const next = safeReturnPath(target);
  return `${LOGIN_PATH}?next=${encodeURIComponent(next)}`;
}
