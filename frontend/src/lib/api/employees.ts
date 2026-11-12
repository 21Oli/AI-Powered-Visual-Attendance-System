import { requestJson, API_PATHS } from "./client";
import { ApiError } from "./errors";
import { parseEmployee, parseEmployeeList } from "./types";
import type { Employee, EmployeeListResponse } from "./types";

/** The backend clamps larger values to 100; asking for 100 keeps pages fewest. */
export const EMPLOYEE_PAGE_LIMIT = 100;

/** Hard stop so a pathological backend can never spin the UI forever. */
export const MAX_EMPLOYEE_PAGES = 100;

export interface EmployeePageOptions {
  readonly after?: string | null;
  readonly limit?: number;
  readonly signal?: AbortSignal;
}

/**
 * One keyset page. Only `after` and `limit` are ever sent, matching the
 * backend's approved query surface.
 */
export async function fetchEmployeePage(
  options: EmployeePageOptions = {},
): Promise<EmployeeListResponse> {
  const limit = options.limit ?? EMPLOYEE_PAGE_LIMIT;
  const search = new URLSearchParams();
  search.set("limit", String(limit));
  if (options.after) {
    search.set("after", options.after);
  }

  return requestJson({
    path: API_PATHS.employees,
    search,
    signal: options.signal,
    parse: parseEmployeeList,
  });
}

/**
 * Walk every keyset page and return the full collection.
 *
 * The page `count` is only the page length, so a total can be trusted **only**
 * after `next_after` is null. A repeated cursor or an over-long walk throws
 * instead of returning a partial collection that would look like a total.
 */
export async function fetchAllEmployees(
  signal?: AbortSignal,
): Promise<Employee[]> {
  const employees: Employee[] = [];
  const seenCursors = new Set<string>();
  let after: string | null = null;

  for (let page = 0; page < MAX_EMPLOYEE_PAGES; page += 1) {
    const response = await fetchEmployeePage({ after, signal });
    employees.push(...response.employees);

    if (response.next_after === null) {
      return employees;
    }
    if (seenCursors.has(response.next_after)) {
      throw new ApiError({
        kind: "malformed",
        code: "PAGINATION_CURSOR_REPEATED",
        message: "The employee list repeated a pagination cursor.",
      });
    }
    seenCursors.add(response.next_after);
    after = response.next_after;
  }

  throw new ApiError({
    kind: "malformed",
    code: "PAGINATION_LIMIT_REACHED",
    message: "The employee list exceeded the safe number of pages.",
  });
}

export interface CreateEmployeeInput {
  readonly employee_id: string;
  readonly full_name: string;
  /** Defaults to `true`; the backend assigns `created_at` itself. */
  readonly is_active?: boolean;
}

/**
 * Create one employee.
 *
 * Only the three fields the backend accepts are sent: `created_at` is
 * application-authoritative and never client-supplied.
 */
export async function createEmployee(
  input: CreateEmployeeInput,
  signal?: AbortSignal,
): Promise<Employee> {
  return requestJson({
    path: API_PATHS.employees,
    method: "POST",
    body: {
      employee_id: input.employee_id,
      full_name: input.full_name,
      is_active: input.is_active ?? true,
    },
    signal,
    parse: parseEmployee,
  });
}

/** One employee by identifier (`404` surfaces as `EMPLOYEE_NOT_FOUND`). */
export async function fetchEmployeeById(
  employeeId: string,
  signal?: AbortSignal,
): Promise<Employee> {
  return requestJson({
    path: API_PATHS.employee(employeeId),
    signal,
    parse: parseEmployee,
  });
}

/** Rename one employee. The body carries `full_name` only. */
export async function renameEmployee(
  employeeId: string,
  fullName: string,
  signal?: AbortSignal,
): Promise<Employee> {
  return requestJson({
    path: API_PATHS.employee(employeeId),
    method: "PATCH",
    body: { full_name: fullName },
    signal,
    parse: parseEmployee,
  });
}

/** Deactivate one employee. The contract has no body and no reactivation. */
export async function deactivateEmployee(
  employeeId: string,
  signal?: AbortSignal,
): Promise<Employee> {
  return requestJson({
    path: API_PATHS.employeeDeactivate(employeeId),
    method: "POST",
    signal,
    parse: parseEmployee,
  });
}
