import type { Metadata } from "next";

import { PagePlaceholder } from "../../../components/layout/PagePlaceholder";

export const metadata: Metadata = { title: "System Status" };

export default function SystemStatusPage() {
  return (
    <PagePlaceholder
      title="System Status"
      purpose="Service availability, so an operator can distinguish a quiet day from an unreachable backend before trusting an empty screen."
      planned={[
        "Backend health result with the time it was checked",
        "Clear offline and unavailable states",
        "Guidance on what to check when the service is down",
      ]}
    />
  );
}
