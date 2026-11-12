import { describe, expect, it } from "vitest";

import { ApiError } from "../src/lib/api/errors";
import {
  mapCreateEmployeeError,
  mapEmployeeMutationError,
  stripLocationPrefix,
} from "../src/lib/employees/errors";

function httpError(
  code: string,
  status: number,
  fields?: ReadonlyArray<{ field: string; message: string }>,
): ApiError {
  return new ApiError({
    kind: "http",
    status,
    code,
    message: `Failure ${code}.`,
    ...(fields === undefined ? {} : { fields }),
  });
}

describe("employee form error mapping", () => {
  it("maps a duplicate employee id to the id field", () => {
    expect(mapCreateEmployeeError(httpError("EMPLOYEE_EXISTS", 409))).toEqual({
      employeeId: "An employee with this ID already exists.",
    });
  });

  it("strips pydantic locations before matching a field", () => {
    expect(stripLocationPrefix("body.full_name")).toBe("full_name");
    expect(stripLocationPrefix("query.limit")).toBe("limit");
    expect(stripLocationPrefix("full_name")).toBe("full_name");

    expect(
      mapCreateEmployeeError(
        httpError("REQUEST_INVALID", 422, [
          { field: "body.full_name", message: "Field required." },
        ]),
      ),
    ).toEqual({ fullName: "Field required." });
  });

  it("maps an employee id field error", () => {
    expect(
      mapCreateEmployeeError(
        httpError("EMPLOYEE_INVALID", 422, [
          { field: "body.employee_id", message: "Already registered." },
        ]),
      ),
    ).toEqual({ employeeId: "Already registered." });
  });

  it("keeps validation errors it cannot map", () => {
    const mapped = mapCreateEmployeeError(
      httpError("REQUEST_INVALID", 422, [
        { field: "body.is_active", message: "Input should be a valid boolean." },
        { field: "body.full_name", message: "Field required." },
      ]),
    );

    expect(mapped.fullName).toBe("Field required.");
    expect(mapped.form).toContain("body.is_active");
    expect(mapped.form).toContain("Input should be a valid boolean.");
  });

  it("does not present a proxy refusal as backend field validation", () => {
    const mapped = mapCreateEmployeeError(
      httpError("PROXY_REQUEST_INVALID", 413),
    );

    expect(mapped.employeeId).toBeUndefined();
    expect(mapped.fullName).toBeUndefined();
    expect(mapped.form).toMatch(/rejected by the dashboard proxy/i);
  });

  it("explains an unavailable service", () => {
    expect(mapCreateEmployeeError(httpError("EMPLOYEE_UNAVAILABLE", 503)).form).toMatch(
      /temporarily unavailable/i,
    );
  });

  it("explains a network failure", () => {
    const error = new ApiError({
      kind: "network",
      code: "NETWORK_UNAVAILABLE",
      message: "The dashboard could not reach its API.",
    });

    expect(mapCreateEmployeeError(error).form).toMatch(/could not reach/i);
  });

  it("maps a missing employee for rename", () => {
    expect(mapEmployeeMutationError(httpError("EMPLOYEE_NOT_FOUND", 404))).toEqual({
      form: "That employee no longer exists.",
    });
  });

  it("maps a rename field error and preserves unmapped ones", () => {
    const mapped = mapEmployeeMutationError(
      httpError("REQUEST_INVALID", 422, [
        { field: "body.full_name", message: "String should have at least 1 character." },
        { field: "body.is_active", message: "Extra inputs are not permitted." },
      ]),
    );

    expect(mapped.fullName).toBe("String should have at least 1 character.");
    expect(mapped.form).toContain("body.is_active");
  });
});
