import type { ReactNode } from "react";

import { RequireAuth } from "../../components/auth/RequireAuth";
import { AppShell } from "../../components/layout/AppShell";

/**
 * Route group layout for the dashboard areas.
 *
 * Every dashboard route renders inside the one shared shell, so the sidebar,
 * header and mobile drawer are never duplicated per page. `RequireAuth` wraps
 * the shell (and therefore every page in it): an unauthenticated visitor is
 * redirected to `/login` before the shell — let alone the page — is mounted.
 */
export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <AppShell>{children}</AppShell>
    </RequireAuth>
  );
}
