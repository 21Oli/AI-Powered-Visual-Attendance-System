import type { Metadata } from "next";

import { PagePlaceholder } from "../../../components/layout/PagePlaceholder";

export const metadata: Metadata = { title: "Attendance" };

export default function AttendancePage() {
  return (
    <PagePlaceholder
      title="Attendance"
      purpose="The attendance journal: recorded check-in and check-out events, incomplete trailing records, and operator-authorized check-out with its cooldown."
      planned={[
        "Journal records with employee names resolved from the register",
        "Notice when the journal ends in an incomplete record",
        "Operator-authorized check-out behind a confirmation step",
      ]}
    />
  );
}
