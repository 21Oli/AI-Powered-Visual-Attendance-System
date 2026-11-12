/**
 * Display formatting for employee records.
 *
 * Dates are rendered as ISO calendar dates in UTC: the API emits UTC ISO-8601
 * timestamps, and a locale-formatted string would vary by machine and invite
 * false precision about the viewer's timezone. `created_at` is nullable by
 * contract, and an unreadable value is never turned into "Invalid Date".
 */

export const MISSING_DATE_LABEL = "Not recorded";
export const UNREADABLE_DATE_LABEL = "Unavailable";

export function formatCreatedAt(value: string | null): string {
  if (value === null) {
    return MISSING_DATE_LABEL;
  }
  const instant = Date.parse(value);
  if (Number.isNaN(instant)) {
    return UNREADABLE_DATE_LABEL;
  }
  return new Date(instant).toISOString().slice(0, 10);
}

export function formatStatus(isActive: boolean): string {
  return isActive ? "Active" : "Inactive";
}
