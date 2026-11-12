import { describe, expect, it } from "vitest";

import {
  formatCreatedAt,
  formatStatus,
  MISSING_DATE_LABEL,
  UNREADABLE_DATE_LABEL,
} from "../src/lib/employees/format";
import {
  MAX_EMPLOYEE_ID_LENGTH,
  describeEmployeeIdProblem,
  describeFullNameProblem,
  employeeIdProblem,
  isManageableEmployeeId,
} from "../src/lib/employees/validation";
import { isSafeEmployeeId } from "../src/lib/server/proxy";

describe("employee id validation", () => {
  it("accepts ordinary identifiers", () => {
    expect(employeeIdProblem("EMP-1")).toBeNull();
    expect(describeEmployeeIdProblem("EMP-1")).toBeNull();
    expect(isManageableEmployeeId("EMP-1")).toBe(true);
  });

  it("preserves leading zeros, hyphens and mixed case", () => {
    for (const id of ["0007", "00123", "emp-0001", "Emp-0001", "e-1-2-3"]) {
      expect(employeeIdProblem(id)).toBeNull();
      expect(isManageableEmployeeId(id)).toBe(true);
    }
  });

  it("allows separators the proxy allows, without normalising them", () => {
    expect(employeeIdProblem("Ada Lovelace 1")).toBeNull();
    expect(employeeIdProblem("EMP_1.2")).toBeNull();
    expect(employeeIdProblem("EMP+1")).toBeNull();
  });

  it("rejects an empty identifier", () => {
    expect(employeeIdProblem("")).toBe("empty");
    expect(describeEmployeeIdProblem("")).toBe("Enter an employee ID.");
  });

  it("rejects surrounding whitespace", () => {
    for (const id of [" EMP-1", "EMP-1 ", "\tEMP-1", "EMP-1\n"]) {
      expect(employeeIdProblem(id)).toBe("surrounding-whitespace");
    }
    expect(describeEmployeeIdProblem(" EMP-1")).toMatch(/remove the spaces/i);
  });

  it("accepts exactly the maximum length and rejects one more", () => {
    const atLimit = "E".repeat(MAX_EMPLOYEE_ID_LENGTH);
    const overLimit = "E".repeat(MAX_EMPLOYEE_ID_LENGTH + 1);

    expect(MAX_EMPLOYEE_ID_LENGTH).toBe(128);
    expect(employeeIdProblem(atLimit)).toBeNull();
    expect(employeeIdProblem(overLimit)).toBe("too-long");
    expect(describeEmployeeIdProblem(overLimit)).toMatch(/128 characters or fewer/i);
  });

  it("rejects path separators, encoding and query markers", () => {
    for (const character of ["/", "\\", "%", "#", "?"]) {
      expect(employeeIdProblem(`EMP${character}1`)).toBe("unsupported-character");
    }
    expect(describeEmployeeIdProblem("EMP%1")).toMatch(/cannot contain/i);
  });

  it("rejects the traversal names and control characters", () => {
    expect(employeeIdProblem(".")).toBe("unsupported-character");
    expect(employeeIdProblem("..")).toBe("unsupported-character");
    expect(employeeIdProblem("EMP\u0000")).toBe("unsupported-character");
    expect(employeeIdProblem("EMP\u007f")).toBe("unsupported-character");
  });

  it("agrees with the proxy rule it delegates to", () => {
    const samples = [
      "EMP-1",
      "",
      " EMP-1",
      "EMP/1",
      ".",
      "..",
      "%",
      "E".repeat(129),
      "Ada Lovelace",
      "0007",
    ];

    for (const sample of samples) {
      expect(isManageableEmployeeId(sample)).toBe(isSafeEmployeeId(sample));
    }
  });

  it("rejects empty or whitespace-only names without altering valid ones", () => {
    expect(describeFullNameProblem("")).toBe("Enter the employee's full name.");
    expect(describeFullNameProblem("   ")).toBe("Enter the employee's full name.");
    expect(describeFullNameProblem("Ada Lovelace")).toBeNull();
    // Padding is the backend's to normalise; the form must not reject it here.
    expect(describeFullNameProblem("  Ada Lovelace  ")).toBeNull();
  });
});

describe("employee formatting", () => {
  it("renders a creation date as an ISO calendar date", () => {
    expect(formatCreatedAt("2026-03-04T10:20:30+00:00")).toBe("2026-03-04");
    expect(formatCreatedAt("2026-03-04T23:59:59+00:00")).toBe("2026-03-04");
  });

  it("uses an explicit placeholder for a missing date", () => {
    expect(formatCreatedAt(null)).toBe(MISSING_DATE_LABEL);
    expect(MISSING_DATE_LABEL).toBe("Not recorded");
  });

  it("never renders Invalid Date", () => {
    expect(formatCreatedAt("not-a-date")).toBe(UNREADABLE_DATE_LABEL);
    expect(formatCreatedAt("")).toBe(UNREADABLE_DATE_LABEL);
  });

  it("labels status", () => {
    expect(formatStatus(true)).toBe("Active");
    expect(formatStatus(false)).toBe("Inactive");
  });
});
