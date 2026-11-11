import type { NextConfig } from "next";

/**
 * Phase 4 — P4.2 scaffold configuration.
 *
 * Deliberately minimal: this milestone adds no rewrites, no headers and no
 * backend proxy. The same-origin Route Handler proxy (`src/app/api/backend`)
 * arrives in P4.8, at which point the backend base URL is read server-side only
 * (never from a `NEXT_PUBLIC_` variable).
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
};

export default nextConfig;
