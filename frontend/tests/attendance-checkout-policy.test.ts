import { describe, expect, it } from "vitest";

import type { Employee } from "../src/lib/api/types";
import {
  CHECKOUT_COOLDOWN_SECONDS,
  CHECKOUT_EXPLANATIONS,
  checkoutEligibility,
  checkoutFailureNotice,
} from "../src/lib/attendance/checkout";
import type { CheckoutFailure } from "../src/lib/attendance/errors";

const employee = (id: string, active = true): Employee => ({
  employee_id: id,
  full_name: `Fixture ${id}`,
  is_active: active,
  created_at: null,
});

const eligibleInput = {
  employee: employee("A"),
  session: "open" as const,
  directoryState: "ready" as const,
  liveData: true,
  pending: false,
};

describe("checkout eligibility", () => {
  it("allows an active, resolved employee with an open session in live mode", () => {
    expect(checkoutEligibility(eligibleInput)).toEqual({ eligible: true });
  });

  it("blocks fixture data first", () => {
    expect(checkoutEligibility({ ...eligibleInput, liveData: false })).toEqual({
      eligible: false,
      reason: "fixture",
      explanation: CHECKOUT_EXPLANATIONS.fixture,
    });
  });

  it("blocks while the directory is loading or unavailable", () => {
    expect(
      checkoutEligibility({ ...eligibleInput, directoryState: "loading" }),
    ).toMatchObject({ eligible: false, reason: "directory-loading" });
    expect(
      checkoutEligibility({ ...eligibleInput, directoryState: "unavailable" }),
    ).toMatchObject({ eligible: false, reason: "directory-unavailable" });
  });

  it("blocks an unresolved employee id", () => {
    expect(checkoutEligibility({ ...eligibleInput, employee: null })).toEqual({
      eligible: false,
      reason: "unresolved",
      explanation: CHECKOUT_EXPLANATIONS.unresolved,
    });
  });

  it("blocks inactive employees", () => {
    expect(
      checkoutEligibility({ ...eligibleInput, employee: employee("A", false) }),
    ).toMatchObject({ eligible: false, reason: "inactive" });
  });

  it("blocks an identifier the proxy cannot address", () => {
    expect(
      checkoutEligibility({ ...eligibleInput, employee: employee("bad%id") }),
    ).toMatchObject({ eligible: false, reason: "unsupported-identifier" });
  });

  it("blocks an unknown session state", () => {
    expect(
      checkoutEligibility({ ...eligibleInput, session: undefined }),
    ).toMatchObject({ eligible: false, reason: "session-unknown" });
  });

  it("blocks a closed session", () => {
    expect(checkoutEligibility({ ...eligibleInput, session: "closed" })).toEqual({
      eligible: false,
      reason: "no-open-session",
      explanation: CHECKOUT_EXPLANATIONS["no-open-session"],
    });
  });

  it("blocks a second in-flight check-out for the same employee", () => {
    expect(checkoutEligibility({ ...eligibleInput, pending: true })).toMatchObject({
      eligible: false,
      reason: "pending",
    });
  });

  it("explains every refusal, so no control is left unexplained", () => {
    for (const [reason, explanation] of Object.entries(CHECKOUT_EXPLANATIONS)) {
      expect(explanation.length).toBeGreaterThan(0);
      expect(reason.length).toBeGreaterThan(0);
    }
  });
});

const failure = (
  kind: CheckoutFailure["kind"],
  overrides: Partial<CheckoutFailure> = {},
): CheckoutFailure => ({
  kind,
  message: "The service refused the request.",
  code: "CODE",
  uncertain: kind === "persistence_unconfirmed",
  ...overrides,
});

describe("checkout failure notices", () => {
  it("refreshes only when the service's view may differ", () => {
    expect(checkoutFailureNotice(failure("employee_not_found")).refresh).toBe(true);
    expect(checkoutFailureNotice(failure("employee_inactive")).refresh).toBe(true);
    expect(checkoutFailureNotice(failure("no_open_session")).refresh).toBe(true);
    expect(checkoutFailureNotice(failure("checkout_cooldown")).refresh).toBe(false);
    expect(checkoutFailureNotice(failure("persistence_rejected")).refresh).toBe(
      false,
    );
    expect(checkoutFailureNotice(failure("persistence_unconfirmed")).refresh).toBe(
      false,
    );
  });

  it("keeps persistence uncertainty uncertain and offers journal verification", () => {
    const notice = checkoutFailureNotice(failure("persistence_unconfirmed"));

    expect(notice.title).toMatch(/uncertain/i);
    expect(notice.detail).toMatch(/may or may not/);
    expect(notice.detail).toMatch(/not retried/i);
    expect(notice.detail).toMatch(/refresh the journal/i);
    expect(notice.verifyJournal).toBe(true);
    expect(notice.refresh).toBe(false);
  });

  it("never marks any other outcome as needing verification", () => {
    const kinds: CheckoutFailure["kind"][] = [
      "employee_not_found",
      "employee_inactive",
      "no_open_session",
      "checkout_cooldown",
      "persistence_rejected",
      "attendance_unavailable",
      "database_unavailable",
      "other",
    ];
    for (const kind of kinds) {
      expect(checkoutFailureNotice(failure(kind)).verifyJournal).toBe(false);
    }
  });

  it("names the configured cooldown without promising a fixed value", () => {
    const notice = checkoutFailureNotice(failure("checkout_cooldown"));

    expect(notice.detail).toContain(`${CHECKOUT_COOLDOWN_SECONDS} seconds`);
    expect(notice.detail).toMatch(/configuration is authoritative/i);
    expect(notice.detail).toMatch(/wrote nothing/i);
  });

  it("passes an unexpected failure through safely", () => {
    const notice = checkoutFailureNotice(
      failure("other", { message: "The service responded with status 500." }),
    );

    expect(notice.title).toBe("Check-out failed");
    expect(notice.detail).toContain("The service responded with status 500.");
    expect(notice.detail).toContain("No check-out is claimed.");
  });
});
