/**
 * Response types for the eight approved backend routes, plus runtime parsers.
 *
 * Every shape below was verified against the FastAPI source
 * (`src/api.py`, `src/api_service.py`, `src/attendance.py`) rather than assumed:
 *
 * * `event_type` values are lowercase: `"check_in"` / `"check_out"`.
 * * `occurred_at` / `created_at` are UTC-normalized ISO-8601 strings
 *   (`datetime.isoformat()`, e.g. `2026-10-22T12:00:00+00:00`).
 * * `created_at` is **nullable** — the payload builder emits `None` when the
 *   row carries no creation timestamp.
 * * The employee list `count` is the length of **that page**, not a total.
 * * The attendance payload has no pagination at all.
 *
 * Everything here is parsed from `unknown`: a malformed response is reported,
 * never coerced into a plausible-looking object.
 */

export interface Employee {
  readonly employee_id: string;
  readonly full_name: string;
  readonly is_active: boolean;
  /** UTC ISO-8601, or `null` when the record has no creation timestamp. */
  readonly created_at: string | null;
}

export interface EmployeeListResponse {
  readonly employees: readonly Employee[];
  /** Length of this page — never the collection total. */
  readonly count: number;
  readonly limit: number;
  /** Exclusive keyset cursor, or `null` on the final page. */
  readonly next_after: string | null;
}

export const EVENT_TYPE_CHECK_IN = "check_in";
export const EVENT_TYPE_CHECK_OUT = "check_out";

export interface AttendanceEvent {
  readonly event_id: string;
  readonly employee_id: string;
  /** `"check_in"` or `"check_out"` for every event the backend writes today. */
  readonly event_type: string;
  /** UTC ISO-8601. */
  readonly occurred_at: string;
}

export interface AttendanceResponse {
  readonly events: readonly AttendanceEvent[];
  readonly count: number;
  readonly incomplete_trailing_record: boolean;
}

export interface HealthResponse {
  readonly status: "ok";
}

export interface FieldError {
  readonly field: string;
  readonly message: string;
}

export interface ErrorDetail {
  readonly code: string;
  readonly message: string;
  /** Present on some business refusals, e.g. checkout `CHECKOUT_COOLDOWN`. */
  readonly reason?: string;
}

export interface ErrorEnvelope {
  readonly error: ErrorDetail;
  /** Top-level `fields` array on request-validation failures (HTTP 422). */
  readonly fields?: readonly FieldError[];
}

// ---------------------------------------------------------------------------
// Runtime guards
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isOptionalString(value: unknown): value is string | null {
  return value === null || isString(value);
}

export function parseEmployee(raw: unknown): Employee | null {
  if (!isRecord(raw)) {
    return null;
  }
  const { employee_id, full_name, is_active, created_at } = raw;
  if (
    !isString(employee_id) ||
    employee_id.length === 0 ||
    !isString(full_name) ||
    typeof is_active !== "boolean" ||
    !isOptionalString(created_at)
  ) {
    return null;
  }
  return { employee_id, full_name, is_active, created_at };
}

export function parseEmployeeList(raw: unknown): EmployeeListResponse | null {
  if (!isRecord(raw)) {
    return null;
  }
  const { employees, count, limit, next_after } = raw;
  if (
    !Array.isArray(employees) ||
    !isNumber(count) ||
    !isNumber(limit) ||
    !isOptionalString(next_after)
  ) {
    return null;
  }
  const parsed: Employee[] = [];
  for (const entry of employees) {
    const employee = parseEmployee(entry);
    if (employee === null) {
      return null;
    }
    parsed.push(employee);
  }
  return { employees: parsed, count, limit, next_after };
}

export function parseAttendanceEvent(raw: unknown): AttendanceEvent | null {
  if (!isRecord(raw)) {
    return null;
  }
  const { event_id, employee_id, event_type, occurred_at } = raw;
  if (
    !isString(event_id) ||
    event_id.length === 0 ||
    !isString(employee_id) ||
    employee_id.length === 0 ||
    !isString(event_type) ||
    event_type.length === 0 ||
    !isString(occurred_at) ||
    occurred_at.length === 0
  ) {
    return null;
  }
  return { event_id, employee_id, event_type, occurred_at };
}

export function parseAttendance(raw: unknown): AttendanceResponse | null {
  if (!isRecord(raw)) {
    return null;
  }
  const { events, count, incomplete_trailing_record } = raw;
  if (
    !Array.isArray(events) ||
    !isNumber(count) ||
    typeof incomplete_trailing_record !== "boolean"
  ) {
    return null;
  }
  const parsed: AttendanceEvent[] = [];
  for (const entry of events) {
    const event = parseAttendanceEvent(entry);
    if (event === null) {
      return null;
    }
    parsed.push(event);
  }
  return {
    events: parsed,
    count,
    incomplete_trailing_record,
  };
}

export function parseHealth(raw: unknown): HealthResponse | null {
  if (!isRecord(raw) || raw.status !== "ok") {
    return null;
  }
  return { status: "ok" };
}

export function parseErrorEnvelope(raw: unknown): ErrorEnvelope | null {
  if (!isRecord(raw) || !isRecord(raw.error)) {
    return null;
  }
  const { code, message, reason } = raw.error;
  if (!isString(code) || !isString(message)) {
    return null;
  }
  if (reason !== undefined && !isString(reason)) {
    return null;
  }

  let fields: FieldError[] | undefined;
  if (raw.fields !== undefined) {
    if (!Array.isArray(raw.fields)) {
      return null;
    }
    fields = [];
    for (const entry of raw.fields) {
      if (!isRecord(entry) || !isString(entry.field) || !isString(entry.message)) {
        return null;
      }
      fields.push({ field: entry.field, message: entry.message });
    }
  }

  const detail: ErrorDetail =
    reason === undefined ? { code, message } : { code, message, reason };
  return fields === undefined ? { error: detail } : { error: detail, fields };
}
