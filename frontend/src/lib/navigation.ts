/**
 * Single source of truth for dashboard navigation.
 *
 * The sidebar, the mobile drawer, the header section label and the tests all
 * read from `NAV_ITEMS`, so a route can never drift between them.
 */

export const NAV_ICON_NAMES = [
  "overview",
  "employees",
  "attendance",
  "system-status",
] as const;

export type NavIconName = (typeof NAV_ICON_NAMES)[number];

export interface NavItem {
  /** Absolute internal route. */
  readonly href: string;
  readonly label: string;
  readonly icon: NavIconName;
  /** One-line description of the area, used on the placeholder pages. */
  readonly purpose: string;
}

/** Where the root route sends visitors. */
export const DEFAULT_ROUTE = "/overview";

export const NAV_ITEMS: readonly NavItem[] = [
  {
    href: "/overview",
    label: "Overview",
    icon: "overview",
    purpose:
      "One place for workforce activity: employee totals, attendance activity and service availability.",
  },
  {
    href: "/employees",
    label: "Employees",
    icon: "employees",
    purpose:
      "The employee register: who is enrolled, which records are active, and where names are maintained.",
  },
  {
    href: "/attendance",
    label: "Attendance",
    icon: "attendance",
    purpose:
      "The attendance journal: recorded events, incomplete trailing records and operator-authorized check-out.",
  },
  {
    href: "/system-status",
    label: "System Status",
    icon: "system-status",
    purpose:
      "Service availability and backend reachability, so an operator can tell a quiet day from an outage.",
  },
];

/**
 * Whether a navigation item represents the current route.
 *
 * Exact match, or a nested path (`/employees/42` belongs to `/employees`).
 * A shared prefix without a path boundary (`/employees-archive`) does not.
 */
export function isActivePath(
  pathname: string | null | undefined,
  href: string,
): boolean {
  if (!pathname) {
    return false;
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** The navigation item for a route, if the route belongs to a dashboard area. */
export function findNavItem(
  pathname: string | null | undefined,
): NavItem | undefined {
  return NAV_ITEMS.find((item) => isActivePath(pathname, item.href));
}
