import { Button } from "../ui/Button";
import styles from "./PaginationControls.module.css";

/** Page sizes the backend accepts: 1–100, clamped above 100. */
export const PAGE_SIZES = [10, 20, 50, 100] as const;

export interface PaginationControlsProps {
  pageIndex: number;
  pageSize: number;
  canGoNext: boolean;
  canGoPrevious: boolean;
  pending: boolean;
  onNext: () => void;
  onPrevious: () => void;
  onPageSizeChange: (size: number) => void;
}

/**
 * Keyset pagination controls.
 *
 * There is no total and no page-count claim: the backend cannot report one, so
 * the controls say "Page N" and rely on `next_after` for the only fact that is
 * actually known — whether another page was offered.
 */
export function PaginationControls({
  pageIndex,
  pageSize,
  canGoNext,
  canGoPrevious,
  pending,
  onNext,
  onPrevious,
  onPageSizeChange,
}: PaginationControlsProps) {
  return (
    <nav className={styles.nav} aria-label="Employee pagination">
      <div className={styles.controls}>
        <Button
          size="sm"
          variant="secondary"
          onClick={onPrevious}
          disabled={!canGoPrevious || pending}
        >
          Previous
        </Button>
        <Button
          size="sm"
          variant="secondary"
          onClick={onNext}
          disabled={!canGoNext || pending}
        >
          Next
        </Button>
      </div>

      <div className={styles.size}>
        <label className={styles.sizeLabel} htmlFor="employee-page-size">
          Rows per page
        </label>
        <select
          id="employee-page-size"
          className={styles.select}
          value={pageSize}
          onChange={(event) => onPageSizeChange(Number(event.target.value))}
          disabled={pending}
        >
          {PAGE_SIZES.map((size) => (
            <option key={size} value={size}>
              {size}
            </option>
          ))}
        </select>
      </div>

      <p className={styles.position} aria-live="polite">
        Page {pageIndex + 1}
        {pending ? " · updating…" : ""}
      </p>
    </nav>
  );
}
