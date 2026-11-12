import type { AttendanceEvent, Employee } from "../../lib/api/types";
import {
  EMPLOYEE_DETAILS_UNAVAILABLE,
  UNKNOWN_EMPLOYEE_LABEL,
  formatEventType,
  formatOccurredAt,
  formatSessionState,
} from "../../lib/attendance/format";
import type { SessionState } from "../../lib/attendance/journal";
import styles from "./AttendanceTable.module.css";

export type DirectoryState = "loading" | "ready" | "unavailable";

export interface AttendanceTableProps {
  events: readonly AttendanceEvent[];
  /** `null` when the directory could not be loaded at all. */
  employees: ReadonlyMap<string, Employee> | null;
  directoryState: DirectoryState;
  /** Derived current session state per employee (latest valid event). */
  sessionStates: ReadonlyMap<string, SessionState>;
}

function EmployeeCell({
  employeeId,
  employees,
  directoryState,
}: {
  employeeId: string;
  employees: ReadonlyMap<string, Employee> | null;
  directoryState: DirectoryState;
}) {
  if (directoryState === "loading") {
    return <span className={styles.muted}>Loading employee…</span>;
  }
  if (directoryState === "unavailable" || employees === null) {
    return (
      <span className={styles.muted}>{EMPLOYEE_DETAILS_UNAVAILABLE}</span>
    );
  }
  const employee = employees.get(employeeId);
  if (employee === undefined) {
    return <span className={styles.muted}>{UNKNOWN_EMPLOYEE_LABEL}</span>;
  }
  return (
    <span className={styles.employeeName}>
      {employee.full_name}
      {employee.is_active ? null : (
        <span className={styles.inactive}>inactive</span>
      )}
    </span>
  );
}

/**
 * Attendance history, in journal append order.
 *
 * The table is never sorted by timestamp: append order is the journal's own
 * order and the only one the contract guarantees. The Session column shows the
 * employee's current derived state (latest valid event), not a per-row session,
 * which the note under the table states explicitly.
 *
 * Below 720px the same table is restyled into stacked rows (each cell carries
 * its column name through `data-label`), so the accessible structure stays a
 * table at every width.
 */
export function AttendanceTable({
  events,
  employees,
  directoryState,
  sessionStates,
}: AttendanceTableProps) {
  return (
    <div className={styles.wrapper}>
      <table className={styles.table}>
        <caption className={styles.caption}>
          Attendance journal, in append order
        </caption>
        <thead>
          <tr>
            <th scope="col">Employee</th>
            <th scope="col">Employee ID</th>
            <th scope="col">Event</th>
            <th scope="col">Timestamp (UTC)</th>
            <th scope="col">Session</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => {
            const type = formatEventType(event.event_type);
            const session = formatSessionState(
              sessionStates.get(event.employee_id),
            );
            return (
              <tr key={event.event_id}>
                <td data-label="Employee">
                  <EmployeeCell
                    employeeId={event.employee_id}
                    employees={employees}
                    directoryState={directoryState}
                  />
                </td>
                <td data-label="Employee ID">
                  <code className={styles.id}>{event.employee_id}</code>
                </td>
                <td data-label="Event">
                  <span
                    className={
                      type.known ? styles.eventKnown : styles.eventUnknown
                    }
                  >
                    {type.label}
                  </span>
                </td>
                <td data-label="Timestamp (UTC)">
                  <span className={styles.timestamp}>
                    {formatOccurredAt(event.occurred_at)}
                  </span>
                </td>
                <td data-label="Session">
                  <span className={styles.session}>{session}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <p className={styles.note}>
        Session shows each employee&apos;s current derived state — the latest
        valid event for that employee — not a separate session per row. Ties on
        the same timestamp go to the event later in the journal.
      </p>
    </div>
  );
}
