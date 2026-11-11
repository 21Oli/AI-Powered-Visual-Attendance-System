import type { ReactNode } from "react";

import { AppShell } from "../../components/layout/AppShell";

/**
 * Route group layout for the dashboard areas.
 *
 * Every dashboard route renders inside the one shared shell, so the sidebar,
 * header and mobile drawer are never duplicated per page.
 */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
