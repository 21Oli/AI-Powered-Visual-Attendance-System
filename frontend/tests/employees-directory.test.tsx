import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EmployeesView } from "../src/components/employees/EmployeesView";

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

interface Row {
  employee_id: string;
  full_name?: string;
  is_active?: boolean;
  created_at?: string | null;
}

function employee(row: Row) {
  return {
    employee_id: row.employee_id,
    full_name: row.full_name ?? `Name ${row.employee_id}`,
    is_active: row.is_active ?? true,
    created_at:
      row.created_at === undefined ? "2026-01-01T00:00:00+00:00" : row.created_at,
  };
}

function page(rows: readonly Row[], next: string | null, limit = 20) {
  const employees = rows.map(employee);
  return { employees, count: employees.length, limit, next_after: next };
}

function urlOf(input: RequestInfo | URL): URL {
  return new URL(String(input), "http://localhost");
}

function routeList(
  handler: (after: string | null, limit: string | null) => Response,
): void {
  fetchMock.mockImplementation((input) => {
    const url = urlOf(input);
    return Promise.resolve(
      handler(url.searchParams.get("after"), url.searchParams.get("limit")),
    );
  });
}

function rowFor(id: string): HTMLElement {
  const cell = screen.getByText(id);
  const row = cell.closest("tr");
  if (!(row instanceof HTMLElement)) {
    throw new Error(`No table row found for "${id}"`);
  }
  return row;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("employee directory", () => {
  it("shows a loading state before the first page resolves", () => {
    fetchMock.mockReturnValue(new Promise<Response>(() => undefined));

    render(<EmployeesView />);

    expect(
      screen.getByRole("heading", { level: 1, name: "Employees" }),
    ).toBeDefined();
    expect(screen.getByText(/loading employees/i)).toBeDefined();
  });

  it("shows an empty state without inventing a total", async () => {
    routeList(() => json(page([], null)));

    render(<EmployeesView />);

    expect(
      await screen.findByText(/no employees are registered yet/i),
    ).toBeDefined();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText(/on this page/).textContent).toBe(
      "Page 1 · 0 on this page",
    );
  });

  it("renders the page with status and creation date", async () => {
    routeList(() =>
      json(
        page(
          [
            {
              employee_id: "EMP-1",
              full_name: "Ada Example",
              created_at: "2026-03-04T10:20:30+00:00",
            },
            { employee_id: "EMP-2", full_name: "Grace Example", is_active: false, created_at: null },
          ],
          null,
        ),
      ),
    );

    render(<EmployeesView />);

    const table = await screen.findByRole("table", {
      name: "Employees on this page",
    });
    expect(within(table).getByText("EMP-1")).toBeDefined();
    expect(within(table).getByText("Ada Example")).toBeDefined();
    expect(within(table).getByText("Grace Example")).toBeDefined();
    expect(within(table).getByText("Active")).toBeDefined();
    expect(within(table).getByText("Inactive")).toBeDefined();
    expect(within(table).getByText("2026-03-04")).toBeDefined();
    expect(within(table).getByText("Not recorded")).toBeDefined();
    expect(within(table).queryByText(/invalid date/i)).toBeNull();
  });

  it("keeps an unsupported identifier visible but unmanageable", async () => {
    routeList(() =>
      json(
        page(
          [{ employee_id: "EMP-1" }, { employee_id: "bad%id", full_name: "Legacy Row" }],
          null,
        ),
      ),
    );

    render(<EmployeesView />);
    await screen.findByRole("table");

    const legacy = rowFor("bad%id");
    expect(within(legacy).getByText("Legacy Row")).toBeDefined();
    expect(within(legacy).getByText(/unsupported id/i)).toBeDefined();
    expect(
      within(legacy).getByRole("button", { name: "View" }).getAttribute("disabled"),
    ).not.toBeNull();
    expect(
      within(legacy)
        .getByRole("button", { name: "Deactivate" })
        .getAttribute("disabled"),
    ).not.toBeNull();

    // A manageable neighbour stays actionable.
    const normal = rowFor("EMP-1");
    expect(
      within(normal).getByRole("button", { name: "View" }).getAttribute("disabled"),
    ).toBeNull();
  });

  it("reports an unavailable directory and retries", async () => {
    let attempts = 0;
    fetchMock.mockImplementation(() => {
      attempts += 1;
      return attempts === 1
        ? Promise.reject(new TypeError("Failed to fetch"))
        : Promise.resolve(json(page([{ employee_id: "EMP-1" }], null)));
    });

    const user = userEvent.setup();
    render(<EmployeesView />);

    expect(
      await screen.findByText(/employee directory is unavailable/i),
    ).toBeDefined();
    expect(screen.getByText(/NETWORK_UNAVAILABLE/)).toBeDefined();

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByRole("table")).toBeDefined();
    expect(screen.queryByText(/employee directory is unavailable/i)).toBeNull();
  });

  it("pages forward with the cursor and back through local history", async () => {
    routeList((after) =>
      after === null
        ? json(page([{ employee_id: "EMP-1" }], "EMP-1"))
        : json(page([{ employee_id: "EMP-2" }], null)),
    );

    const user = userEvent.setup();
    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(await screen.findByText("EMP-2")).toBeDefined();
    expect(screen.queryByText("EMP-1")).toBeNull();
    expect(screen.getByText("Page 2 · 1 on this page")).toBeDefined();

    const nextUrl = urlOf(fetchMock.mock.calls[1]?.[0] as RequestInfo);
    expect(nextUrl.searchParams.get("after")).toBe("EMP-1");
    expect(nextUrl.searchParams.get("limit")).toBe("20");

    await user.click(screen.getByRole("button", { name: "Previous" }));

    expect(await screen.findByText("EMP-1")).toBeDefined();
    const backUrl = urlOf(fetchMock.mock.calls[2]?.[0] as RequestInfo);
    expect(backUrl.searchParams.get("after")).toBeNull();
    expect(screen.getByText("Page 1 · 1 on this page")).toBeDefined();
  });

  it("disables Previous on the first page", async () => {
    routeList(() => json(page([{ employee_id: "EMP-1" }], null)));

    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    expect(
      screen.getByRole("button", { name: "Previous" }).getAttribute("disabled"),
    ).not.toBeNull();
    expect(
      screen.getByRole("button", { name: "Next" }).getAttribute("disabled"),
    ).not.toBeNull();
  });

  it("resets to the first page when the page size changes", async () => {
    routeList((after, limit) =>
      after === null
        ? json(page([{ employee_id: "EMP-1" }], "EMP-1", Number(limit)))
        : json(page([{ employee_id: "EMP-2" }], null, Number(limit))),
    );

    const user = userEvent.setup();
    render(<EmployeesView />);
    await screen.findByText("EMP-1");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("EMP-2");

    await user.selectOptions(screen.getByLabelText(/rows per page/i), "50");

    await waitFor(() => {
      expect(screen.getByText("Page 1 · 1 on this page")).toBeDefined();
    });
    const lastUrl = urlOf(fetchMock.mock.calls.at(-1)?.[0] as RequestInfo);
    expect(lastUrl.searchParams.get("limit")).toBe("50");
    expect(lastUrl.searchParams.get("after")).toBeNull();
  });

  it("handles a full final page followed by an empty page", async () => {
    routeList((after) =>
      after === null
        ? json(page([{ employee_id: "EMP-1" }, { employee_id: "EMP-2" }], "EMP-2"))
        : json(page([], null)),
    );

    const user = userEvent.setup();
    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(await screen.findByText(/this page is empty/i)).toBeDefined();
    expect(screen.queryByRole("table")).toBeNull();
    expect(
      screen.getByText(/the previous page was full/i),
    ).toBeDefined();

    await user.click(
      screen.getByRole("button", { name: /back to the previous page/i }),
    );

    expect(await screen.findByText("EMP-1")).toBeDefined();
  });

  it("never presents the page count as a total", async () => {
    routeList(() => json(page([{ employee_id: "EMP-1" }, { employee_id: "EMP-2" }], "EMP-2")));

    render(<EmployeesView />);
    await screen.findByText("EMP-1");

    expect(screen.getByText("Page 1 · 2 on this page")).toBeDefined();
    const nav = screen.getByRole("navigation", { name: "Employee pagination" });
    expect(nav.textContent ?? "").not.toMatch(/\bof\b/i);
    expect(screen.queryByText(/\bof\s+\d+\b/)).toBeNull();
  });

  it("ignores a stale list response that resolves after a newer one", async () => {
    let listCalls = 0;
    let resolveFirst: (response: Response) => void = () => undefined;
    const firstList = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });

    fetchMock.mockImplementation((input, init) => {
      if (init?.method === "POST") {
        return Promise.resolve(json(employee({ employee_id: "NEW" }), 201));
      }
      listCalls += 1;
      return listCalls === 1
        ? firstList
        : Promise.resolve(json(page([{ employee_id: "FRESH" }], null)));
    });

    const user = userEvent.setup();
    render(<EmployeesView />);

    await user.click(screen.getByRole("button", { name: "Create employee" }));
    await user.type(screen.getByLabelText(/employee id/i), "NEW");
    await user.type(screen.getByLabelText(/full name/i), "New Person");
    await user.click(screen.getByRole("button", { name: "Create employee" }));

    expect(await screen.findByText("FRESH")).toBeDefined();

    resolveFirst(json(page([{ employee_id: "STALE" }], null)));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(screen.queryByText("STALE")).toBeNull();
    expect(screen.getByText("FRESH")).toBeDefined();
  });
});
