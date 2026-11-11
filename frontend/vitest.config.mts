import { defineConfig } from "vitest/config";

/**
 * Vitest configuration for the approved testing stack
 * (vitest 5.1.0 + jsdom 31.0.0 + @testing-library/react 16.3.3).
 *
 * JSX needs no plugin and no `esbuild.jsx` override: Next.js sets
 * `jsx: "react-jsx"` in `tsconfig.json`, which Vite's esbuild transform honours,
 * so the automatic runtime is used without introducing `@vitejs/plugin-react`.
 */
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: false,
    include: ["tests/**/*.test.{ts,tsx}"],
    setupFiles: ["./tests/setup.ts"],
    restoreMocks: true,
    clearMocks: true,
  },
});
