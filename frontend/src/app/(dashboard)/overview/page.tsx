import type { Metadata } from "next";

import { PagePlaceholder } from "../../../components/layout/PagePlaceholder";

export const metadata: Metadata = { title: "Overview" };

export default function OverviewPage() {
  return (
    <PagePlaceholder
      title="Overview"
      purpose="A single view of workforce activity: employee totals, the day's attendance activity and service availability, all read from the backend rather than estimated."
      planned={[
        "Employee totals with active and inactive counts",
        "Attendance activity for the current day",
        "Service availability at a glance",
      ]}
    />
  );
}
