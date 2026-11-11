# Frontend — Phase 4 dashboard

Next.js 16 (App Router) + TypeScript + CSS Modules dashboard for the
AI-Powered Visual Attendance System.

**Status: P4.2 complete — scaffold and design system only.**
There are no dashboard screens, no navigation shell and no backend proxy yet.
Those arrive in P4.3–P4.8 and require owner approval at each milestone.

---

## Requirements

| Tool | Version used |
|---|---|
| Node.js | v24.21.0 (minimum declared: `>=20.9.0`) |
| npm | 11.19.0 |

## Install and run

```bash
cd frontend
npm ci            # reproducible install from package-lock.json
npm run dev       # http://localhost:3000
```

`npm install` also works, but `npm ci` is preferred because every version is
pinned exactly (no `^` or `~` ranges).

## Scripts

| Script | Command | Purpose |
|---|---|---|
| `dev` | `next dev` | development server |
| `build` | `next build` | production build |
| `start` | `next start` | serve the production build |
| `lint` | `eslint .` | ESLint flat config (`next lint` was removed in Next 16) |
| `typecheck` | `tsc --noEmit` | strict TypeScript, no emit |
| `test` | `vitest run` | unit + component tests (jsdom) |
| `test:watch` | `vitest` | watch mode |

## Dependencies (exact pins)

Production: `next 16.4.6`, `react 19.3.1`, `react-dom 19.3.1`.

Development: `typescript 5.9.3`, `@types/node 24.19.2`, `@types/react 19.3.0`,
`@types/react-dom 19.3.0`, `eslint 10.14.0`, `eslint-config-next 16.4.6`,
`vitest 5.1.0`, `vite 8.3.8`, `jsdom 31.0.0`, `@testing-library/react 16.3.3`,
`@testing-library/dom 10.4.2`, `@testing-library/user-event 14.6.9`.

No Tailwind, no UI framework, no icon package, no state library, no CSS-in-JS.
`vite` is pinned explicitly because Vitest 5 declares it as a required (not
optional) peer. JSX in tests needs no React plugin: Next sets
`"jsx": "react-jsx"` in `tsconfig.json`, which Vite's esbuild transform honours.

## Structure

```text
frontend/
├── public/
│   ├── icons/                     # empty (P4.3)
│   └── images/                    # empty (P4.3)
├── src/
│   ├── app/
│   │   ├── layout.tsx             # root layout, header, theme bootstrap
│   │   ├── page.tsx               # landing placeholder
│   │   ├── globals.css            # design tokens + base styles
│   │   ├── loading.tsx            # route loading state
│   │   └── error.tsx              # sanitized route error boundary
│   ├── components/ui/
│   │   ├── Button.tsx             # button primitive (4 variants, 2 sizes)
│   │   └── ThemeToggle.tsx        # accessible theme control
│   ├── hooks/
│   │   └── useTheme.ts            # subscribes to the theme store
│   ├── lib/
│   │   ├── theme.ts               # pure theme rules + bootstrap script
│   │   └── themeStore.ts          # external store over the DOM
│   └── styles/                    # reserved for shared module sheets (P4.3)
├── tests/                         # Vitest suites
├── eslint.config.mjs
├── next.config.ts
├── tsconfig.json
├── vitest.config.mts
└── package.json
```

## Design tokens

All visual decisions live in `src/app/globals.css` as CSS custom properties and
are consumed by CSS Modules — components never hard-code a colour.

* **Colour:** `--color-bg`, `--color-bg-raised`, `--color-surface`,
  `--color-surface-hover`, `--color-border`, `--color-border-strong`,
  `--color-text`, `--color-text-muted`, `--color-text-subtle`,
  `--color-accent` (teal), `--color-accent-hover`, `--color-accent-contrast`,
  `--color-focus`, `--color-success`, `--color-warning`, `--color-danger`,
  `--color-overlay`.
* **Spacing:** `--space-1` … `--space-8` (4 px base).
* **Typography:** `--font-sans`, `--font-mono`, `--text-xs` … `--text-3xl`,
  `--leading-tight`, `--leading-normal`, `--weight-regular|medium|semibold`.
* **Shape:** `--radius-sm|md|lg|pill`.
* **Depth:** `--shadow-sm`, `--shadow-md`, `--shadow-focus`.
* **Motion:** `--transition-fast`, `--transition-base`.
* **Layering:** `--z-header`.

Every colour token is declared **twice** — once in the dark block
(`:root, [data-theme="dark"]`) and once in `[data-theme="light"]` — so switching
themes can never leave a value unresolved. `tests/design-tokens.test.ts` fails
if a required token is missing from either block.

## Theme implementation

Three cooperating pieces, chosen so there is **no flash and no hydration
mismatch**:

1. **Pre-paint bootstrap** (`THEME_INIT_SCRIPT` in `src/lib/theme.ts`, injected in
   `layout.tsx` `<head>`): reads the stored preference, falls back to
   `prefers-color-scheme`, and sets `data-theme` plus `color-scheme` on `<html>`
   synchronously before first paint.
2. **External store** (`src/lib/themeStore.ts`): the document is the source of
   truth. `getSnapshot()` reads `data-theme`; `subscribe()` also listens for OS
   scheme changes while the preference is `system`. Persistence and `matchMedia`
   failures degrade quietly instead of breaking the page.
3. **Hook** (`src/hooks/useTheme.ts`): `useSyncExternalStore` with
   `getServerSnapshot() === null`, so the server and the first client render
   agree. State is never mirrored in an effect, which also satisfies
   `react-hooks/set-state-in-effect`.

Precedence: an explicit stored choice (`light`/`dark`) always wins; otherwise the
operating system decides. Toggle cycles between the two explicit themes and
persists the choice under `attendance-dashboard-theme`.

Accessibility: real `<button>` with a theme-aware `aria-label`, keyboard
operable, visible `:focus-visible` ring, icon-only below 480 px while keeping the
accessible name, and `prefers-reduced-motion` honoured globally.

## Tests

```bash
npm run test
```

| Suite | Tests | Covers |
|---|---|---|
| `tests/theme.test.ts` | 10 | preference parsing, resolution precedence, apply, storage failure, OS detection, bootstrap script |
| `tests/theme-toggle.test.tsx` | 6 | mount application, SSR safety, switching, persistence, stored-value precedence, keyboard operation |
| `tests/home-page.test.tsx` | 2 | single `<h1>`, scaffold facts, no navigation/data claims |
| `tests/design-tokens.test.ts` | 38 | every colour token in both themes, shared tokens, focus ring, reduced motion, dark default |

Measured on 2026-11-11 (Node v24.21.0, npm 11.19.0):

| Check | Result |
|---|---|
| `npm ci` | exit 0 |
| `npm ls --depth=0` | exit 0, 15 packages, no unmet peers |
| `npm run lint` | exit 0, 14 s, 0 problems |
| `npm run typecheck` | exit 0, 9 s |
| `npm run test` | exit 0, 12.5 s, **56 passed / 56** (4 files) |
| `npm run build` | exit 0, 22 s, static `/` and `/_not-found` |

## Fixture policy

**No fixture data exists in this milestone**, and none is needed for the
scaffold.

The owner approved offline development with clearly labelled, development-only
fixtures for later milestones. When fixtures are introduced (P4.4 onwards) they
must be:

* development-only, and impossible to select silently in a production build;
* labelled `FIXTURE DATA` in the UI wherever they appear;
* never mixed with a real backend response in the same view;
* never written to the backend, the database or the attendance journal.

## Known limitations

* No dashboard screens, navigation shell, API client or proxy yet — `/` is a
  placeholder.
* No live backend verification. The FastAPI service enforces the G2+G8 schema
  startup gate, which needs a PostgreSQL connection, so the dashboard is verified
  offline only.
* `eslint-config-next@16.4.6` bundles `eslint-plugin-react@7.37.5` and
  `eslint-plugin-jsx-a11y@6.10.2`, whose declared peers stop at ESLint 9. npm
  resolved the peer ranges itself (warnings only, no `--force`, no
  `--legacy-peer-deps`) and lint runs clean on ESLint 10.14.0.
* `npm run test` reports that jsdom is created once per test file (~75 % of test
  time); this is informational and no `pool` tuning has been applied.
* No authentication or RBAC (Phase 5); the backend remains loopback-only with
  CORS disabled.

## Safety

This frontend reads no credential, holds no secret and never contacts the
database. `BACKEND_BASE_URL` is introduced as a server-only variable in P4.8; no
`NEXT_PUBLIC_` value may ever carry a backend URL or secret.
