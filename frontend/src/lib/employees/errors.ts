import type { ApiError } from "../api/errors";

/**
 * Mapping from API failures to employee form fields.
 *
 * Backend validation paths arrive as pydantic locations (`body.full_name`), so
 * the location prefix is stripped before matching. A field that cannot be
 * mapped is kept as a general message instead of being dropped, and proxy
 * failures are never presented as backend field validation.
 */
export interface EmployeeFormErrors {
  employeeId?: string;
  fullName?: string;
  form?: string;
}

const LOCATION_PREFIX = /^(body|query|path|cookie|header)\./;

export function stripLocationPrefix(field: string): string {
  return field.replace(LOCATION_PREFIX, "");
}

function mapFields(error: ApiError): EmployeeFormErrors | null {
  if (error.fields === undefined || error.fields.length === 0) {
    return null;
  }
  const mapped: EmployeeFormErrors = {};
  const unmapped: string[] = [];

  for (const entry of error.fields) {
    const field = stripLocationPrefix(entry.field);
    if (field === "employee_id") {
      mapped.employeeId = entry.message;
    } else if (field === "full_name") {
      mapped.fullName = entry.message;
    } else {
      unmapped.push(`${entry.field}: ${entry.message}`);
    }
  }

  if (unmapped.length > 0) {
    mapped.form = unmapped.join(" ");
  }
  return mapped.employeeId === undefined &&
    mapped.fullName === undefined &&
    mapped.form === undefined
    ? null
    : mapped;
}

function genericMessage(error: ApiError): string {
  if (error.code === "PROXY_REQUEST_INVALID" || error.status === 413) {
    return "The request was rejected by the dashboard proxy before it reached the service.";
  }
  if (error.code.startsWith("BACKEND_") || error.code === "PROXY_INTERNAL_ERROR") {
    return "The dashboard could not reach the attendance service.";
  }
  if (error.status === 503) {
    return "The service is temporarily unavailable. Nothing was saved.";
  }
  if (error.kind === "network") {
    return "The dashboard could not reach its API.";
  }
  return error.message;
}

/** Failure while creating an employee. */
export function mapCreateEmployeeError(error: ApiError): EmployeeFormErrors {
  const fields = mapFields(error);
  if (fields !== null) {
    return fields;
  }
  if (error.code === "EMPLOYEE_EXISTS") {
    return { employeeId: "An employee with this ID already exists." };
  }
  if (error.code === "EMPLOYEE_NOT_FOUND") {
    return { form: "That employee no longer exists." };
  }
  return { form: genericMessage(error) };
}

/** Failure while renaming (or reading) one employee. */
export function mapEmployeeMutationError(error: ApiError): EmployeeFormErrors {
  const fields = mapFields(error);
  if (fields !== null) {
    return fields;
  }
  if (error.code === "EMPLOYEE_NOT_FOUND") {
    return { form: "That employee no longer exists." };
  }
  return { form: genericMessage(error) };
}
