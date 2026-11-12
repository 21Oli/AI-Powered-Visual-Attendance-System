import {
  ATTENDANCE_PAGE_SIZES,
  normaliseAttendancePageSize,
} from "../../lib/attendance/pagination";
import { Button } from "../ui/Button";
import styles from "./AttendancePagination.module.css";

export interface AttendancePaginationProps {
  page: number;
  totalPages: number;
  pageSize: number;
  totalItems: number;
  /** One-based index of the first visible item, or 0 when there are none. */
  firstItem: number;
  /** One-based index of the last visible item, or 0 when there are none. */
  lastItem: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}

/**
 * Pagination over the **filtered, loaded** journal.
 *
 * The totals describe the client-side result set, never a server page: the
 * attendance endpoint has no pagination parameters, so no additional attendance
 * requests are made here.
 */
export function AttendancePagination({
  page,
  totalPages,
  pageSize,
  totalItems,
  firstItem,
  lastItem,
  onPageChange,
  onPageSizeChange,
}: AttendancePaginationProps) {
  const range =
    totalItems === 0
      ? "No events on this page"
      : `Events ${firstItem}–${lastItem} of ${totalItems}`;

  return (
    <nav className={styles.nav} aria-label="Attendance pagination">
      <div className={styles.controls}>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1}
        >
          Previous
        </Button>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages}
        >
          Next
        </Button>
      </div>

      <div className={styles.size}>
        <label className={styles.sizeLabel} htmlFor="attendance-page-size">
          Rows per page
        </label>
        <select
          id="attendance-page-size"
          className={styles.select}
          value={pageSize}
          onChange={(event) =>
            onPageSizeChange(
              normaliseAttendancePageSize(Number(event.target.value)),
            )
          }
        >
          {ATTENDANCE_PAGE_SIZES.map((size) => (
            <option key={size} value={size}>
              {size}
            </option>
          ))}
        </select>
      </div>

      <p className={styles.position} aria-live="polite">
        Page {page} of {totalPages} · {range}
      </p>
    </nav>
  );
}
