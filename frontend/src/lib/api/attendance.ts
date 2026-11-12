import { requestJson, API_PATHS } from "./client";
import {
  EVENT_TYPE_CHECK_OUT,
  parseAttendance,
  parseAttendanceEvent,
} from "./types";
import type { AttendanceEvent, AttendanceResponse } from "./types";

/**
 * The whole validated journal.
 *
 * The backend exposes **no** attendance pagination, so nothing here sends
 * `after`/`limit`; inventing them would be a lie about the contract.
 */
export async function fetchAttendance(
  signal?: AbortSignal,
): Promise<AttendanceResponse> {
  return requestJson({
    path: API_PATHS.attendance,
    signal,
    parse: parseAttendance,
  });
}

/**
 * Operator-authorized check-out for one employee.
 *
 * The contract has **no** request body, so none is sent (not even `{}`): no
 * employee data, timestamp or authorization metadata travels in the request —
 * the employee comes from the path and the operator authority from the service.
 *
 * The 201 payload must be the event the service says it appended, so beyond the
 * shared shape validation it must be a `check_out` **for the requested
 * employee**. Anything else is treated as a malformed success and rejects,
 * rather than being reported as a check-out that did not happen.
 */
export async function checkoutEmployee(
  employeeId: string,
  signal?: AbortSignal,
): Promise<AttendanceEvent> {
  return requestJson({
    path: API_PATHS.checkout(employeeId),
    method: "POST",
    signal,
    parse: (raw) => {
      const event = parseAttendanceEvent(raw);
      if (event === null) {
        return null;
      }
      if (event.employee_id !== employeeId) {
        return null;
      }
      if (event.event_type !== EVENT_TYPE_CHECK_OUT) {
        return null;
      }
      return event;
    },
  });
}
