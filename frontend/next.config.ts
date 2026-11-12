import type { NextConfig } from "next";

/**
 * Phase 4 — P4.2 scaffold configuration.
 *
 * Deliberately minimal: no rewrites, no headers and no alternate proxy. The
 * same-origin Route Handler proxy (`src/app/api/backend`) was added in P4.4 and
 * reads the backend base URL server-side only (never from a `NEXT_PUBLIC_`
 * variable).
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
};

export default nextConfig;
