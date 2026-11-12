import {
  MAX_EMPLOYEE_ID_LENGTH,
  isSafeEmployeeId,
} from "../server/proxy";

/**
 * Form validation for employee input.
 *
 * The identifier rule is **not** re-implemented here: `isSafeEmployeeId` is
 * imported from the proxy policy module so the form accepts exactly what the
 * proxy can address. This is deliberate — a duplicated rule would drift, and a
 * drift would let an operator create an employee the dashboard could never
 * fetch, rename or deactivate. The imported module contains validation helpers
 * only: no environment value, URL or secret.
 *
 * An id is otherwise preserved exactly: no trimming, collapsing or case
 * folding is applied before submission.
 */

export { MAX_EMPLOYEE_ID_LENGTH };

export type EmployeeIdProblem =
  | "empty"
  | "surrounding-whitespace"
  | "too-long"
  | "unsupported-character";

/** Classify an identifier, or `null` when the proxy can address it. */
export function employeeIdProblem(value: string): EmployeeIdProblem | null {
  if (value.length === 0) {
    return "empty";
  }
  if (value !== value.trim()) {
    return "surrounding-whitespace";
  }
  if (value.length > MAX_EMPLOYEE_ID_LENGTH) {
    return "too-long";
  }
  // Everything else (including `.`, `..` and control characters) is decided by
  // the proxy's own rule, never by a second copy of it.
  return isSafeEmployeeId(value) ? null : "unsupported-character";
}

/** Whether this dashboard can manage (read, rename, deactivate) the id. */
export function isManageableEmployeeId(value: string): boolean {
  return employeeIdProblem(value) === null;
}

/** Human-readable field error, or `null` when the value is acceptable. */
export function describeEmployeeIdProblem(value: string): string | null {
  switch (employeeIdProblem(value)) {
    case null:
      return null;
    case "empty":
      return "Enter an employee ID.";
    case "surrounding-whitespace":
      return "Remove the spaces before or after the employee ID.";
    case "too-long":
      return `Use ${MAX_EMPLOYEE_ID_LENGTH} characters or fewer.`;
    case "unsupported-character":
      return "The ID cannot contain / \\ % # ? or control characters, and cannot be '.' or '..'. The dashboard's API proxy cannot address such an ID.";
  }
}

/** Full name must not be empty or whitespace-only. */
export function describeFullNameProblem(value: string): string | null {
  return value.trim().length === 0
    ? "Enter the employee's full name."
    : null;
}
