import nextConfig from "eslint-config-next/core-web-vitals";

/**
 * ESLint flat configuration for Next.js 16.
 *
 * `eslint-config-next` 16 ships a ready-made flat config array (no
 * `@eslint/eslintrc` / `FlatCompat` bridge required), so no extra package is
 * introduced. `next lint` was removed in Next 16, which is why the `lint`
 * script runs `eslint .` directly.
 */
const config = [
  ...nextConfig,
  {
    ignores: ["node_modules/**", ".next/**", "out/**", "coverage/**"],
  },
];

export default config;
