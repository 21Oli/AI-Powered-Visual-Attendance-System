import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AttendanceView } from "../src/components/attendance/AttendanceView";
import type { AttendanceEvent, Employee } from "../src/lib/api/types";
import { CHECKOUT_COOLDOWN_SECONDS } from "../src/lib/attendance/checkout";

const fetchMock = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function apiError(status: number, code: string, reason?: string): Response {
  return json(
    {
      error: {
        code,
        message: "The service refused the request.",
        ...(reason === undefined ? {} : { reason }),
      },
    },
    status,
  );
}

const event = (
  id: string,
  employeeId: string,
  type: string,
  occurredAt: string,
): AttendanceEvent => ({
  event_id: id,
  employee_id: employeeId,
  event_type: type,
  occurred_at: occurredAt,
});

const employee = (id: string, name: string, active = true): Employee => ({
  employee_id: id,
  full_name: name,
  is_active: active,
  created_at: null,
});

function employeesPage(rows: readonly Employee[], next: string | null) {
  return { employees: rows, count: rows.length, limit: 100, next_after: next };
}

function attendancePayload(events: readonly AttendanceEvent[], incomplete = false) {
  return { events, count: events.length, incomplete_trailing_record: incomplete };
}

interface Handlers {
  attendance?: () => Promise<Response>;
  employees?: () => Promise<Response>;
  checkout?: (employeeId: string) => Promise<Response>;
}

function routeApi(handlers: Handlers = {}) {
  fetchMock.mockImplementation((input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "POST") {
      const match = /\/attendance\/(.+)\/checkout$/.exec(url);
      const employeeId = match?.[1] === undefined ? "" : decodeURIComponent(match[1]);
      return handlers.checkout === undefined
        ? Promise.resolve(json({}, 201))
        : handlers.checkout(employeeId);
    }
    if (url.includes("/employees")) {
      return handlers.employees === undefined
        ? Promise.resolve(json(employeesPage([], null)))
        : handlers.employees();
    }
    return handlers.attendance === undefined
      ? Promise.resolve(json(attendancePayload([])))
      : handlers.attendance();
  });
}

function getCount(part: string): number {
  return fetchMock.mock.calls.filter(
    ([input, init]) =>
      String(input).includes(part) &&
      (init?.method ?? "GET").toUpperCase() === "GET",
  ).length;
}

function postCalls() {
  return fetchMock.mock.calls.filter(
    ([, init]) => (init?.method ?? "GET").toUpperCase() === "POST",
  );
}

const CHECKED_OUT: AttendanceEvent = event(
  "e9",
  "A",
  "check_out",
  "2026-11-11T09:00:00Z",
);

const journal: AttendanceEvent[] = [
  event("e1", "A", "check_in", "2026-11-11T08:00:00Z"),
  event("e2", "B", "check_in", "2026-11-11T08:10:00Z"),
  event("e3", "C", "check_in", "2026-11-11T08:20:00Z"),
  event("e4", "C", "check_out", "2026-11-11T08:30:00Z"),
  event("e5", "D", "lunch_break", "2026-11-11T08:40:00Z"),
  event("e6", "UNRESOLVED", "check_in", "2026-11-11T08:50:00Z"),
];

const register: Employee[] = [
  employee("A", "Ada Fixture"),
  employee("B", "Grace Fixture", false),
  employee("C", "Closed Fixture"),
  employee("D", "Unknown State"),
];

function routeJournal(handlers: Handlers = {}) {
  routeApi({
    attendance: () => Promise.resolve(json(attendancePayload(journal))),
    employees: () => Promise.resolve(json(employeesPage(register, null))),
    ...handlers,
  });
}

async function renderAndReady(
  handlers: Handlers = {},
): Promise<{ user: ReturnType<typeof userEvent.setup>; panel: HTMLElement }> {
  routeJournal(handlers);
  const user = userEvent.setup();
  render(<AttendanceView dataSourceKind="live" />);
  const panel = await screen.findByRole("region", { name: "Operator checkout" });
  await within(panel).findByRole("button", { name: "Check out Ada Fixture" });
  return { user, panel };
}

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("checkout eligibility", () => {
  it("offers check-out only for an active employee with an open session", async () => {
    const { panel } = await renderAndReady();

    expect(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    ).toBeDefined();
  });

  it("explains every ineligible employee and offers no button", async () => {
    const { panel } = await renderAndReady();

    expect(
      within(panel).queryByRole("button", { name: "Check out Grace Fixture" }),
    ).toBeNull();
    expect(
      within(panel).getByText("Inactive employees cannot be checked out."),
    ).toBeDefined();

    expect(
      within(panel).queryByRole("button", { name: "Check out Closed Fixture" }),
    ).toBeNull();
    expect(
      within(panel).getAllByText(
        "The journal shows no open session for this employee.",
      ),
    ).toHaveLength(1);

    expect(
      within(panel).queryByRole("button", { name: "Check out Unknown State" }),
    ).toBeNull();
    expect(
      within(panel).getByText(
        "The journal shows no reliable current session for this employee.",
      ),
    ).toBeDefined();

    expect(
      within(panel).queryByRole("button", { name: /^Check out Unknown employee$/ }),
    ).toBeNull();
    expect(
      within(panel).getByText("This employee is not in the loaded register."),
    ).toBeDefined();
  });

  it("withholds every action when the directory is unavailable", async () => {
    routeApi({
      attendance: () => Promise.resolve(json(attendancePayload(journal))),
      employees: () => Promise.reject(new TypeError("Failed to fetch")),
    });
    render(<AttendanceView dataSourceKind="live" />);

    const panel = await screen.findByRole("region", {
      name: "Operator checkout",
    });
    expect(
      await within(panel).findByText(/no check-out can be authorised/i),
    ).toBeDefined();
    expect(within(panel).queryAllByRole("button")).toHaveLength(0);
  });

  it("withholds actions while the directory is still loading", async () => {
    const pendingEmployees = deferred<Response>();
    routeApi({
      attendance: () => Promise.resolve(json(attendancePayload(journal))),
      employees: () => pendingEmployees.promise,
    });
    render(<AttendanceView dataSourceKind="live" />);

    const panel = await screen.findByRole("region", {
      name: "Operator checkout",
    });
    expect(
      within(panel).getByText(/still loading, so check-out is unavailable/i),
    ).toBeDefined();
    expect(within(panel).queryAllByRole("button")).toHaveLength(0);

    pendingEmployees.resolve(json(employeesPage(register, null)));
    expect(
      await within(panel).findByRole("button", { name: "Check out Ada Fixture" }),
    ).toBeDefined();
  });

  it("refuses an identifier the proxy cannot address", async () => {
    routeApi({
      attendance: () =>
        Promise.resolve(
          json(
            attendancePayload([
              event("e1", "bad%id", "check_in", "2026-11-11T08:00:00Z"),
            ]),
          ),
        ),
      employees: () =>
        Promise.resolve(json(employeesPage([employee("bad%id", "Percent Fixture")], null))),
    });
    render(<AttendanceView dataSourceKind="live" />);

    const panel = await screen.findByRole("region", { name: "Operator checkout" });
    expect(
      within(panel).queryByRole("button", { name: /^Check out Percent Fixture$/ }),
    ).toBeNull();
    expect(
      within(panel).getByText(
        "This identifier cannot be addressed through the dashboard's API proxy.",
      ),
    ).toBeDefined();
  });

  it("disables check-out completely in fixture mode", async () => {
    render(<AttendanceView dataSourceKind="fixture" />);

    const panel = await screen.findByRole("region", { name: "Operator checkout" });
    expect(
      within(panel).getByText(/requires live backend data/i),
    ).toBeDefined();
    expect(within(panel).queryAllByRole("button")).toHaveLength(0);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole("note").textContent).toContain("FIXTURE DATA");
  });
});

describe("checkout confirmation", () => {
  it("opens a dialog that identifies the employee and the operator semantics", async () => {
    const { user, panel } = await renderAndReady();

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );

    const dialog = screen.getByRole("dialog", {
      name: "Check out Ada Fixture?",
    });
    expect(within(dialog).getByText("Ada Fixture")).toBeDefined();
    expect(within(dialog).getByText("A")).toBeDefined();
    expect(within(dialog).getByText(/operator-authorized/i)).toBeDefined();
    expect(
      within(dialog).getByText(/records a check-out attendance event/i),
    ).toBeDefined();
    expect(
      within(dialog).getByText(
        new RegExp(`${CHECKOUT_COOLDOWN_SECONDS} seconds`),
      ),
    ).toBeDefined();
    expect(within(dialog).getByText(/cannot undo/i)).toBeDefined();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDefined();
    expect(within(dialog).getByRole("button", { name: "Check out" })).toBeDefined();
    expect(postCalls()).toHaveLength(0);
  });

  it("cancel closes the dialog without sending a request", async () => {
    const { user, panel } = await renderAndReady();

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(postCalls()).toHaveLength(0);
  });

  it("confirm sends exactly one bodyless POST without a body content type", async () => {
    const { user, panel } = await renderAndReady({
      checkout: () => Promise.resolve(json(CHECKED_OUT, 201)),
    });

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Check out" }));

    await screen.findByText(/was checked out at/);

    const posts = postCalls();
    expect(posts).toHaveLength(1);
    expect(String(posts[0]?.[0])).toBe("/api/backend/api/v1/attendance/A/checkout");
    expect(posts[0]?.[1]?.body).toBeUndefined();
    const headers = new Headers(posts[0]?.[1]?.headers);
    expect(headers.get("content-type")).toBeNull();
    expect(headers.get("accept")).toBe("application/json");
  });

  it("keeps one request in flight and ignores repeated confirms", async () => {
    const pending = deferred<Response>();
    const { user, panel } = await renderAndReady({
      checkout: () => pending.promise,
    });

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    const confirm = screen.getByRole("button", { name: "Check out" });
    await user.click(confirm);

    expect(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }).getAttribute(
        "disabled",
      ),
    ).not.toBeNull();
    expect(within(panel).getByText(/Check-out pending for A/)).toBeDefined();
    expect(
      screen.getByRole("button", { name: "Working…" }).getAttribute("disabled"),
    ).not.toBeNull();
    expect(screen.getByRole("button", { name: "Cancel" }).getAttribute("disabled")).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "Working…" }));
    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    expect(postCalls()).toHaveLength(1);

    pending.resolve(json(CHECKED_OUT, 201));
    await screen.findByText(/was checked out at/);
    expect(postCalls()).toHaveLength(1);
  });

  it("keeps other employees usable while one check-out is pending", async () => {
    routeApi({
      attendance: () =>
        Promise.resolve(
          json(
            attendancePayload([
              event("e1", "A", "check_in", "2026-11-11T08:00:00Z"),
              event("e2", "E", "check_in", "2026-11-11T08:15:00Z"),
            ]),
          ),
        ),
      employees: () =>
        Promise.resolve(
          json(
            employeesPage(
              [employee("A", "Ada Fixture"), employee("E", "Eve Fixture")],
              null,
            ),
          ),
        ),
      checkout: () => pending.promise,
    });
    const pending = deferred<Response>();
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    const panel = await screen.findByRole("region", { name: "Operator checkout" });

    await user.click(
      await within(panel).findByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Check out" }));

    expect(
      within(panel).getByRole("button", { name: "Check out Eve Fixture" }).getAttribute(
        "disabled",
      ),
    ).toBeNull();

    pending.resolve(json(CHECKED_OUT, 201));
    await screen.findByText(/was checked out at/);
  });
});

describe("checkout success", () => {
  it("refreshes the journal and removes eligibility only after success", async () => {
    let attendanceCalls = 0;
    const { user, panel } = await renderAndReady({
      attendance: () => {
        attendanceCalls += 1;
        return Promise.resolve(
          json(
            attendancePayload(
              attendanceCalls === 1 ? journal : [...journal, CHECKED_OUT],
            ),
          ),
        );
      },
      checkout: () => Promise.resolve(json(CHECKED_OUT, 201)),
    });

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Check out" }));

    expect(await screen.findByText(/was checked out at/)).toBeDefined();
    await waitFor(() => {
      expect(getCount("/attendance")).toBe(2);
    });
    await waitFor(() => {
      expect(
        within(panel).queryByRole("button", { name: "Check out Ada Fixture" }),
      ).toBeNull();
    });
    expect(
      within(panel).getAllByText(
        "The journal shows no open session for this employee.",
      ).length,
    ).toBeGreaterThan(0);

    const table = screen.getByRole("table", {
      name: "Attendance journal, in append order",
    });
    expect(within(table).getByText("2026-11-11 09:00:00 UTC")).toBeDefined();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("never claims success or closes the session while the request is pending", async () => {
    const pending = deferred<Response>();
    const { user, panel } = await renderAndReady({
      checkout: () => pending.promise,
    });

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Check out" }));

    expect(screen.queryByText(/was checked out at/)).toBeNull();
    const table = screen.getByRole("table", {
      name: "Attendance journal, in append order",
    });
    expect(within(table).queryByText("2026-11-11 09:00:00 UTC")).toBeNull();
    // A, B and the unresolved id all have a check-in as their latest event.
    expect(within(table).getAllByText("Open")).toHaveLength(3);

    pending.resolve(json(CHECKED_OUT, 201));
    await screen.findByText(/was checked out at/);
  });
});

describe("checkout failures", () => {
  const cases: {
    name: string;
    response: () => Response;
    title: RegExp;
    detail: RegExp;
    reference: RegExp;
    refreshes: boolean;
  }[] = [
    {
      name: "EMPLOYEE_NOT_FOUND",
      response: () => apiError(404, "EMPLOYEE_NOT_FOUND"),
      title: /Employee not found/,
      detail: /no longer has this employee/i,
      reference: /Reference: EMPLOYEE_NOT_FOUND/,
      refreshes: true,
    },
    {
      name: "EMPLOYEE_INACTIVE",
      response: () => apiError(409, "CHECKOUT_REJECTED", "EMPLOYEE_INACTIVE"),
      title: /Employee is inactive/,
      detail: /refused the check-out because the employee is inactive/i,
      reference: /Reference: CHECKOUT_REJECTED \(HTTP 409\) · EMPLOYEE_INACTIVE/,
      refreshes: true,
    },
    {
      name: "NO_OPEN_SESSION",
      response: () => apiError(409, "CHECKOUT_REJECTED", "NO_OPEN_SESSION"),
      title: /No open session/,
      detail: /found no open attendance session/i,
      reference: /Reference: CHECKOUT_REJECTED \(HTTP 409\) · NO_OPEN_SESSION/,
      refreshes: true,
    },
    {
      name: "CHECKOUT_COOLDOWN",
      response: () => apiError(409, "CHECKOUT_REJECTED", "CHECKOUT_COOLDOWN"),
      title: /cooldown is still in effect/,
      detail: /inside its configured cooldown window/i,
      reference: /Reference: CHECKOUT_REJECTED \(HTTP 409\) · CHECKOUT_COOLDOWN/,
      refreshes: false,
    },
    {
      name: "PERSISTENCE_REJECTED",
      response: () => apiError(422, "PERSISTENCE_REJECTED"),
      title: /Check-out was not recorded/,
      detail: /rejected the check-out before persisting it/i,
      reference: /Reference: PERSISTENCE_REJECTED/,
      refreshes: false,
    },
    {
      name: "ATTENDANCE_UNAVAILABLE",
      response: () => apiError(503, "ATTENDANCE_UNAVAILABLE"),
      title: /Attendance service unavailable/,
      detail: /attendance journal is unavailable/i,
      reference: /Reference: ATTENDANCE_UNAVAILABLE/,
      refreshes: false,
    },
    {
      name: "DATABASE_UNAVAILABLE",
      response: () => apiError(503, "DATABASE_UNAVAILABLE"),
      title: /Employee register unavailable/,
      detail: /employee register is unavailable/i,
      reference: /Reference: DATABASE_UNAVAILABLE/,
      refreshes: false,
    },
  ];

  for (const testCase of cases) {
    it(`presents ${testCase.name} distinctly`, async () => {
      const { user, panel } = await renderAndReady({
        checkout: () => Promise.resolve(testCase.response()),
      });

      await user.click(
        within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
      );
      await user.click(screen.getByRole("button", { name: "Check out" }));

      expect(await screen.findByText(testCase.title)).toBeDefined();
      expect(screen.getByText(testCase.detail)).toBeDefined();
      expect(screen.getByText(testCase.reference)).toBeDefined();
      expect(screen.queryByText(/was checked out at/)).toBeNull();
      expect(postCalls()).toHaveLength(1);

      await waitFor(() => {
        expect(getCount("/attendance")).toBe(testCase.refreshes ? 2 : 1);
      });

      const table = screen.getByRole("table", {
        name: "Attendance journal, in append order",
      });
      expect(within(table).queryByText("2026-11-11 09:00:00 UTC")).toBeNull();
    });
  }

  it("treats PERSISTENCE_UNCONFIRMED as uncertain and never retries", async () => {
    const { user, panel } = await renderAndReady({
      checkout: () =>
        Promise.resolve(apiError(503, "PERSISTENCE_UNCONFIRMED")),
    });

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Check out" }));

    expect(
      await screen.findByText(/Check-out outcome is uncertain/),
    ).toBeDefined();
    expect(screen.getByText(/may or may not be in the journal/i)).toBeDefined();
    expect(screen.queryByText(/was checked out at/)).toBeNull();
    expect(screen.queryByText(/Check-out was not recorded/)).toBeNull();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    expect(getCount("/attendance")).toBe(1);
    expect(postCalls()).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Refresh journal" }));
    await waitFor(() => {
      expect(getCount("/attendance")).toBe(2);
    });
    expect(postCalls()).toHaveLength(1);
  });

  it("reports a network failure without inventing an outcome", async () => {
    const { user, panel } = await renderAndReady({
      checkout: () => Promise.reject(new TypeError("Failed to fetch")),
    });

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Check out" }));

    expect(await screen.findByText(/Check-out failed/)).toBeDefined();
    expect(screen.getByText(/No check-out is claimed/)).toBeDefined();
    expect(screen.getByText(/Reference: NETWORK_UNAVAILABLE/)).toBeDefined();
    expect(screen.queryByText(/was checked out at/)).toBeNull();
  });

  it("reports a proxy or server failure safely", async () => {
    const { user, panel } = await renderAndReady({
      checkout: () =>
        Promise.resolve(
          json(
            {
              error: {
                code: "BACKEND_UNAVAILABLE",
                message: "The backend service could not be reached.",
              },
            },
            502,
          ),
        ),
    });

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Check out" }));

    expect(await screen.findByText(/Check-out failed/)).toBeDefined();
    expect(
      screen.getByText(/The backend service could not be reached/),
    ).toBeDefined();
    expect(screen.getByText(/Reference: BACKEND_UNAVAILABLE/)).toBeDefined();
    expect(screen.queryByText(/was checked out at/)).toBeNull();
  });

  it("rejects a malformed success body instead of claiming a check-out", async () => {
    const { user, panel } = await renderAndReady({
      checkout: () => Promise.resolve(json({}, 201)),
    });

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Check out" }));

    expect(await screen.findByText(/Check-out failed/)).toBeDefined();
    expect(
      screen.getByText(/unexpected response shape/i),
    ).toBeDefined();
    expect(screen.getByText(/Reference: MALFORMED_RESPONSE/)).toBeDefined();
    expect(screen.queryByText(/was checked out at/)).toBeNull();
    expect(getCount("/attendance")).toBe(1);
  });

  it("rejects a check-out event for another employee", async () => {
    const { user, panel } = await renderAndReady({
      checkout: () =>
        Promise.resolve(
          json(event("other", "B", "check_out", "2026-11-11T09:00:00Z"), 201),
        ),
    });

    await user.click(
      within(panel).getByRole("button", { name: "Check out Ada Fixture" }),
    );
    await user.click(screen.getByRole("button", { name: "Check out" }));

    expect(await screen.findByText(/Check-out failed/)).toBeDefined();
    expect(screen.queryByText(/was checked out at/)).toBeNull();
  });
});

describe("attendance page regressions with checkout present", () => {
  it("keeps filters, pagination and the torn-journal warning working", async () => {
    routeApi({
      attendance: () =>
        Promise.resolve(
          json(attendancePayload([...journal], true)),
        ),
      employees: () => Promise.resolve(json(employeesPage(register, null))),
    });
    const user = userEvent.setup();
    render(<AttendanceView dataSourceKind="live" />);
    await screen.findByRole("table", {
      name: "Attendance journal, in append order",
    });

    expect(screen.getByText("Journal integrity warning")).toBeDefined();
    expect(screen.getByText("Showing 6 of 6 journal events")).toBeDefined();

    await user.selectOptions(screen.getByLabelText("Event type"), "check_out");
    expect(screen.getByText("Showing 1 of 6 journal events")).toBeDefined();

    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    await user.selectOptions(screen.getByLabelText("Rows per page"), "10");
    expect(screen.getByText(/Page 1 of 1/)).toBeDefined();
  });
});
