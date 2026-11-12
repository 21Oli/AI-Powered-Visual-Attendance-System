"use client";

import type { Employee } from "../../lib/api/types";
import { ALL_ATTENDANCE_FILTERS } from "../../lib/attendance/filters";
import type { AttendanceFilters as Filters } from "../../lib/attendance/filters";
import { formatUtcDayOption } from "../../lib/attendance/format";
import { Button } from "../ui/Button";
import styles from "./AttendanceFilters.module.css";
import type { DirectoryState } from "./AttendanceTable";

export interface AttendanceFiltersProps {
  filters: Filters;
  onChange: (next: Filters) => void;
  utcDays: readonly string[];
  /** `null` when the directory is not available as a filter source. */
  employees: readonly Employee[] | null;
  directoryState: DirectoryState;
  matchingCount: number;
  totalCount: number;
}

function isDefault(filters: Filters): boolean {
  return (
    filters.utcDay === ALL_ATTENDANCE_FILTERS.utcDay &&
    filters.eventType === ALL_ATTENDANCE_FILTERS.eventType &&
    filters.employeeId === ALL_ATTENDANCE_FILTERS.employeeId &&
    filters.sessionState === ALL_ATTENDANCE_FILTERS.sessionState
  );
}

/**
 * The four approved filters.
 *
 * All filtering is client-side over the loaded journal snapshot (the backend
 * exposes no attendance query parameters), so the count line says how many of
 * the loaded records match rather than implying a server query. The employee
 * list is offered only from a successfully loaded directory: an unavailable or
 * partial directory is never presented as a complete employee filter.
 */
export function AttendanceFilters({
  filters,
  onChange,
  utcDays,
  employees,
  directoryState,
  matchingCount,
  totalCount,
}: AttendanceFiltersProps) {
  const reset = () => onChange(ALL_ATTENDANCE_FILTERS);

  return (
    <section className={styles.filters} aria-labelledby="attendance-filters-title">
      <div className={styles.head}>
        <h2 className={styles.title} id="attendance-filters-title">
          Filters
        </h2>
        <p className={styles.count} aria-live="polite">
          Showing {matchingCount} of {totalCount} journal event
          {totalCount === 1 ? "" : "s"}
        </p>
      </div>

      <div className={styles.grid}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="attendance-date-filter">
            Date (UTC)
          </label>
          <select
            id="attendance-date-filter"
            className={styles.select}
            value={filters.utcDay ?? ""}
            onChange={(event) =>
              onChange({
                ...filters,
                utcDay:
                  event.target.value === "" ? null : event.target.value,
              })
            }
          >
            <option value="">All dates</option>
            {utcDays.map((day) => (
              <option key={day} value={day}>
                {formatUtcDayOption(day)}
              </option>
            ))}
          </select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor="attendance-type-filter">
            Event type
          </label>
          <select
            id="attendance-type-filter"
            className={styles.select}
            value={filters.eventType}
            onChange={(event) =>
              onChange({
                ...filters,
                eventType: event.target
                  .value as Filters["eventType"],
              })
            }
          >
            <option value="all">All event types</option>
            <option value="check_in">Check-in</option>
            <option value="check_out">Check-out</option>
          </select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor="attendance-employee-filter">
            Employee
          </label>
          <select
            id="attendance-employee-filter"
            className={styles.select}
            value={filters.employeeId ?? ""}
            onChange={(event) =>
              onChange({
                ...filters,
                employeeId:
                  event.target.value === "" ? null : event.target.value,
              })
            }
            disabled={employees === null}
          >
            <option value="">All employees</option>
            {(employees ?? []).map((employee) => (
              <option key={employee.employee_id} value={employee.employee_id}>
                {employee.full_name} ({employee.employee_id})
              </option>
            ))}
          </select>
          {employees === null ? (
            <p className={styles.hint}>
              {directoryState === "loading"
                ? "Employee-specific filtering is available once the directory loads."
                : "Employee-specific filtering is unavailable because the directory could not be loaded."}
            </p>
          ) : null}
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor="attendance-session-filter">
            Session status
          </label>
          <select
            id="attendance-session-filter"
            className={styles.select}
            value={filters.sessionState}
            onChange={(event) =>
              onChange({
                ...filters,
                sessionState: event.target
                  .value as Filters["sessionState"],
              })
            }
          >
            <option value="all">All statuses</option>
            <option value="open">Open</option>
            <option value="closed">Closed</option>
          </select>
          <p className={styles.hint}>
            Current state per employee; records with no derivable state are not
            listed under Open or Closed.
          </p>
        </div>
      </div>

      <div className={styles.actions}>
        <Button
          variant="secondary"
          size="sm"
          onClick={reset}
          disabled={isDefault(filters)}
        >
          Reset filters
        </Button>
      </div>
    </section>
  );
}

