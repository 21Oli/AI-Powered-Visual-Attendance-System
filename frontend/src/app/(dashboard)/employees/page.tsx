import type { Metadata } from "next";

import { EmployeesView } from "../../../components/employees/EmployeesView";

export const metadata: Metadata = { title: "Employees" };

export default function EmployeesPage() {
  return <EmployeesView />;
}
