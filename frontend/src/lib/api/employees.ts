import { requestJson, API_PATHS } from "./client";
import { ApiError } from "./errors";
import { parseEmployeeList } from "./types";
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
