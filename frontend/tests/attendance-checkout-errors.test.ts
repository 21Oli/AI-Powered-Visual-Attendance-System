import { describe, expect, it } from "vitest";

import { ApiError } from "../src/lib/api/errors";
import { classifyCheckoutError } from "../src/lib/attendance/errors";

function httpError(
  code: string,
  status: number,
  reason?: string,
): ApiError {
  return new ApiError({
    kind: "http",
    status,
    code,
    message: `Failure ${code}.`,
    ...(reason === undefined ? {} : { reason }),
  });
}

describe("checkout failure classification", () => {
  it("classifies a missing employee", () => {
    const failure = classifyCheckoutError(
      httpError("EMPLOYEE_NOT_FOUND", 404, "EMPLOYEE_NOT_FOUND"),
    );

    expect(failure.kind).toBe("employee_not_found");
    expect(failure.code).toBe("EMPLOYEE_NOT_FOUND");
    expect(failure.status).toBe(404);
    expect(failure.reason).toBe("EMPLOYEE_NOT_FOUND");
    expect(failure.uncertain).toBe(false);
    expect(failure.message).toBe("That employee no longer exists.");
  });

  it.each([
    ["EMPLOYEE_INACTIVE", "employee_inactive", /inactive/i],
    ["NO_OPEN_SESSION", "no_open_session", /no open session/i],
    ["CHECKOUT_COOLDOWN", "checkout_cooldown", /cooldown/i],
  ])("distinguishes the 409 reason %s", (reason, kind, message) => {
    const failure = classifyCheckoutError(
      httpError("CHECKOUT_REJECTED", 409, reason),
    );

    expect(failure.kind).toBe(kind);
    expect(failure.code).toBe("CHECKOUT_REJECTED");
    expect(failure.reason).toBe(reason);
    expect(failure.uncertain).toBe(false);
    expect(failure.message).toMatch(message);
  });

  it("does not guess a refusal with a missing or unknown reason", () => {
    const missing = classifyCheckoutError(httpError("CHECKOUT_REJECTED", 409));
    const unknown = classifyCheckoutError(
      httpError("CHECKOUT_REJECTED", 409, "SOMETHING_ELSE"),
    );

    expect(missing.kind).toBe("other");
    expect(missing.reason).toBeUndefined();
    expect(unknown.kind).toBe("other");
    expect(unknown.reason).toBe("SOMETHING_ELSE");
  });

  it("classifies a confirmed persistence rejection", () => {
    const failure = classifyCheckoutError(
      httpError("PERSISTENCE_REJECTED", 422),
    );

    expect(failure.kind).toBe("persistence_rejected");
    expect(failure.status).toBe(422);
    expect(failure.uncertain).toBe(false);
    expect(failure.message).toMatch(/journal is unchanged/i);
  });

  it("marks an unconfirmed persistence outcome as uncertain", () => {
    const failure = classifyCheckoutError(
      httpError("PERSISTENCE_UNCONFIRMED", 503),
    );

    expect(failure.kind).toBe("persistence_unconfirmed");
    expect(failure.code).toBe("PERSISTENCE_UNCONFIRMED");
    expect(failure.status).toBe(503);
    expect(failure.uncertain).toBe(true);
    expect(failure.message).toMatch(/may or may not/i);
    expect(failure.message).toMatch(/not retried/i);
  });

  it("never conflates the two persistence outcomes", () => {
    const rejected = classifyCheckoutError(
      httpError("PERSISTENCE_REJECTED", 422),
    );
    const unconfirmed = classifyCheckoutError(
      httpError("PERSISTENCE_UNCONFIRMED", 503),
    );

    expect(rejected.kind).not.toBe(unconfirmed.kind);
    expect(rejected.uncertain).not.toBe(unconfirmed.uncertain);
    expect(rejected.message).not.toBe(unconfirmed.message);
  });

  it.each([
    ["ATTENDANCE_UNAVAILABLE", "attendance_unavailable", /journal is unavailable/i],
    ["DATABASE_UNAVAILABLE", "database_unavailable", /register is unavailable/i],
  ])("distinguishes the service failure %s", (code, kind, message) => {
    const failure = classifyCheckoutError(httpError(code, 503));

    expect(failure.kind).toBe(kind);
    expect(failure.status).toBe(503);
    expect(failure.uncertain).toBe(false);
    expect(failure.message).toMatch(message);
  });

  it.each([
    ["INTERNAL_ERROR", 500],
    ["BACKEND_UNAVAILABLE", 502],
    ["BACKEND_TIMEOUT", 504],
    ["HTTP_ERROR", 502],
    ["SOMETHING_NEW", 409],
  ])("leaves %s out of the business classifications", (code, status) => {
    const failure = classifyCheckoutError(httpError(code, status));

    expect(failure.kind).toBe("other");
    expect(failure.uncertain).toBe(false);
    expect(failure.code).toBe(code);
  });

  it("does not classify a network failure as a business refusal", () => {
    const failure = classifyCheckoutError(
      new ApiError({
        kind: "network",
        code: "NETWORK_UNAVAILABLE",
        message: "The dashboard could not reach its API.",
      }),
    );

    expect(failure.kind).toBe("other");
    expect(failure.status).toBeUndefined();
    expect(failure.message).toBe("The dashboard could not reach its API.");
  });

  it("does not classify a malformed success payload as a refusal", () => {
    const failure = classifyCheckoutError(
      new ApiError({
        kind: "malformed",
        code: "MALFORMED_RESPONSE",
        message: "The service returned an unexpected response shape.",
      }),
    );

    expect(failure.kind).toBe("other");
    expect(failure.uncertain).toBe(false);
  });

  it("matches on code and status together, not the reason alone", () => {
    const failure = classifyCheckoutError(
      httpError("CHECKOUT_REJECTED", 404, "NO_OPEN_SESSION"),
    );

    expect(failure.kind).toBe("other");
  });

  it("is deterministic and does not mutate the error", () => {
    const error = httpError("CHECKOUT_REJECTED", 409, "CHECKOUT_COOLDOWN");
    const before = {
      code: error.code,
      status: error.status,
      reason: error.reason,
      message: error.message,
    };

    const first = classifyCheckoutError(error);
    const second = classifyCheckoutError(error);

    expect(first).toEqual(second);
    expect({
      code: error.code,
      status: error.status,
      reason: error.reason,
      message: error.message,
    }).toEqual(before);
  });
});
