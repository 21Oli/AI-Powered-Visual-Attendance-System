import type { Metadata } from "next";

import { PagePlaceholder } from "../../../components/layout/PagePlaceholder";

export const metadata: Metadata = { title: "Employees" };

export default function EmployeesPage() {
  return (
    <PagePlaceholder
      title="Employees"
      purpose="The employee register: browse the enrolled workforce, create a record, correct a name, and deactivate someone who has left."
      planned={[
        "Paged employee list with keyset navigation",
        "Create employee with duplicate-name and validation feedback",
        "Rename an employee and deactivate a record",
      ]}
    />
  );
}
