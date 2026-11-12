import type { AttendanceEvent } from "../api/types";

/**
 * Pure client-side pagination for the filtered attendance journal.
 *
 * Attendance has no server pagination by contract, so paging happens over the
 * already-loaded snapshot. Nothing here mutates the source list, and every
 * returned page is a plain slice in journal order.
 */

/** Page sizes the Attendance page offers (backend employee maximum is 100). */
export const ATTENDANCE_PAGE_SIZES = [10, 20, 50, 100] as const;

export const DEFAULT_ATTENDANCE_PAGE_SIZE = 20;

export type AttendancePageSize = (typeof ATTENDANCE_PAGE_SIZES)[number];

export interface AttendancePage {
  /** Slice for the resolved page (empty only when the source is empty). */
  readonly items: readonly AttendanceEvent[];
  /** One-based page number, always within `1…totalPages`. */
  readonly page: number;
  readonly pageSize: AttendancePageSize;
  readonly totalItems: number;
  /** At least 1, so an empty list still renders as "Page 1 of 1". */
  readonly totalPages: number;
  readonly hasPrevious: boolean;
  readonly hasNext: boolean;
}

/** Fall back to the default size when the requested one is not supported. */
export function normaliseAttendancePageSize(
  value: number | null | undefined,
): AttendancePageSize {
  const match = ATTENDANCE_PAGE_SIZES.find((size) => size === value);
  return match ?? DEFAULT_ATTENDANCE_PAGE_SIZE;
}

/**
 * Paginate the filtered journal.
 *
 * A non-finite, zero or negative page resolves to page 1, and a page past the
 * end resolves to the last page rather than an empty one — so a filter change
 * that shortens the list can never strand the view past its own content.
 */
export function paginateAttendanceEvents(
  events: readonly AttendanceEvent[],
  requestedPage = 1,
  requestedPageSize: number = DEFAULT_ATTENDANCE_PAGE_SIZE,
): AttendancePage {
  const pageSize = normaliseAttendancePageSize(requestedPageSize);
  const totalItems = events.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));

  const safeRequested = Number.isFinite(requestedPage)
    ? Math.trunc(requestedPage)
    : 1;
  const page = Math.min(Math.max(1, safeRequested), totalPages);

  const start = (page - 1) * pageSize;
  return {
    items: events.slice(start, start + pageSize),
    page,
    pageSize,
    totalItems,
    totalPages,
    hasPrevious: page > 1,
    hasNext: page < totalPages,
  };
}
