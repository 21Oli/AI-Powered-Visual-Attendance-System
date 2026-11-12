import type { Metadata } from "next";

import { AttendanceView } from "../../../components/attendance/AttendanceView";
import { resolveDataSourceKind } from "../../../lib/api/data-source";

export const metadata: Metadata = { title: "Attendance" };

/**
 * The journal must reflect the live service, and the data-source decision is
 * made on the server (fixtures can never be enabled from the browser). Both
 * require a per-request render, so the page is never statically cached.
 */
export const dynamic = "force-dynamic";

export default function AttendancePage() {
  const dataSourceKind = resolveDataSourceKind({
    NODE_ENV: process.env.NODE_ENV,
    DASHBOARD_DATA_MODE: process.env.DASHBOARD_DATA_MODE,
  });

  return <AttendanceView dataSourceKind={dataSourceKind} />;
}
