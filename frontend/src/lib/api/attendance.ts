import { requestJson, API_PATHS } from "./client";
import { parseAttendance } from "./types";
import type { AttendanceResponse } from "./types";

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
