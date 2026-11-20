import type { Metadata } from "next";

import { RequirePermission } from "../../../components/auth/RequirePermission";
import { EmployeesView } from "../../../components/employees/EmployeesView";

export const metadata: Metadata = { title: "Employees" };

/**
 * The register needs `employees:read`, the same permission the backend requires
 * on every route this page calls. A role without it sees the access-denied
 * screen instead of a directory of failed requests.
 */
export default function EmployeesPage() {
  return (
    <RequirePermission permission="employees:read">
      <EmployeesView />
    </RequirePermission>
  );
}
