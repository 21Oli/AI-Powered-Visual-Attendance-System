import type { Employee } from "../../lib/api/types";
import { formatCreatedAt, formatStatus } from "../../lib/employees/format";
import { isManageableEmployeeId } from "../../lib/employees/validation";
import { Button } from "../ui/Button";
import styles from "./EmployeeTable.module.css";

export interface EmployeeTableProps {
  employees: readonly Employee[];
  onView: (employee: Employee) => void;
  onDeactivate: (employee: Employee) => void;
}

/**
 * Semantic table of the current page.
 *
 * Below 720px the same table is restyled into stacked rows (each cell carries
 * its column name through `data-label`), so the markup — and therefore the
 * screen-reader structure — stays a table at every width instead of becoming a
 * second, divergent DOM tree.
 *
 * An employee whose identifier the proxy cannot address is still listed, with
 * the reason shown and its actions disabled: hiding a real record would be a
 * worse lie than admitting the dashboard cannot manage it.
 */
export function EmployeeTable({
  employees,
  onView,
  onDeactivate,
}: EmployeeTableProps) {
  return (
    <table className={styles.table}>
      <caption className={styles.caption}>Employees on this page</caption>
      <thead>
        <tr>
          <th scope="col">Employee ID</th>
          <th scope="col">Full name</th>
          <th scope="col">Status</th>
          <th scope="col">Created (UTC)</th>
          <th scope="col">Actions</th>
        </tr>
      </thead>
      <tbody>
        {employees.map((employee) => {
          const manageable = isManageableEmployeeId(employee.employee_id);
          return (
            <tr key={employee.employee_id}>
              <td data-label="Employee ID">
                <code className={styles.id}>{employee.employee_id}</code>
                {manageable ? null : (
                  <span className={styles.unsupported}>
                    Unsupported ID — this employee cannot be viewed, renamed or
                    deactivated because the dashboard proxy cannot address it.
                  </span>
                )}
              </td>
              <td data-label="Full name">{employee.full_name}</td>
              <td data-label="Status">
                <span
                  className={employee.is_active ? styles.active : styles.inactive}
                >
                  {formatStatus(employee.is_active)}
                </span>
              </td>
              <td data-label="Created (UTC)">
                {formatCreatedAt(employee.created_at)}
              </td>
              <td data-label="Actions">
                <div className={styles.actions}>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => onView(employee)}
                    disabled={!manageable}
                  >
                    View
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => onDeactivate(employee)}
                    disabled={!manageable || !employee.is_active}
                  >
                    Deactivate
                  </Button>
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
