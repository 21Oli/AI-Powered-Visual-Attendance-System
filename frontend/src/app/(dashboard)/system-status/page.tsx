import type { Metadata } from "next";

import { resolveDataSourceKind } from "../../../lib/api/data-source";
import { SystemStatusView } from "../../../components/system-status/SystemStatusView";

export const metadata: Metadata = { title: "System Status" };

/**
 * Fixture mode is resolved on the server so the browser cannot enable it, and a
 * production build ignores `DASHBOARD_DATA_MODE` entirely. The page is dynamic
 * because the health check must never be served from a cached render.
 */
export const dynamic = "force-dynamic";

export default function SystemStatusPage() {
  return (
    <SystemStatusView dataSourceKind={resolveDataSourceKind(process.env)} />
  );
}
