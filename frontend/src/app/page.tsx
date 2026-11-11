import { redirect } from "next/navigation";

import { DEFAULT_ROUTE } from "../lib/navigation";

/**
 * The dashboard root forwards to the Overview area, so `/` is never an empty
 * shell without a selected section.
 */
export default function RootPage(): never {
  redirect(DEFAULT_ROUTE);
}
