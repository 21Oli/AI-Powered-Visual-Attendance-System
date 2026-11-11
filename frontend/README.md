# Frontend — Phase 4 dashboard

Next.js 16 (App Router) + TypeScript + CSS Modules dashboard for the
AI-Powered Visual Attendance System.

**Status: P4.3 complete — application shell and navigation.**
The four dashboard areas exist as structural placeholders inside a shared shell.
No employee data, attendance data, health results or backend calls exist yet;
those arrive in P4.4–P4.8 and require owner approval at each milestone.

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
npm run dev       # http://localhost:3000 → redirects to /overview
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

## Routes

| Route | Area | State |
|---|---|---|
| `/` | — | redirects to `/overview` |
| `/overview` | Overview | structural placeholder |
| `/employees` | Employees | structural placeholder |
| `/attendance` | Attendance | structural placeholder |
| `/system-status` | System Status | structural placeholder |
| any other path | — | `not-found` page with a link back to Overview |

Every dashboard route lives in the `src/app/(dashboard)/` route group, so the
sidebar, header and mobile drawer are rendered once by `(dashboard)/layout.tsx`
instead of being duplicated per page. Routes are directly addressable and work
with browser back/forward; `next/link` handles internal navigation.

## Application shell

```text
┌───────────────┬────────────────────────────────────────┐
│ Sidebar       │ Header: section label · theme toggle    │
│ (≥900px)      ├────────────────────────────────────────┤
│ brand         │ main                                   │
│ Primary nav   │   route content                        │
└───────────────┴────────────────────────────────────────┘
```

* **`AppShell`** owns the layout and the only client state: whether the mobile
  drawer is open. It renders the skip link, sidebar, header, drawer and `<main>`.
* **`Sidebar`** is the persistent desktop navigation (hidden below 900 px).
  Active state comes from `isActivePath()`, and the current link carries
  `aria-current="page"` — colour is never the only signal.
* **`Header`** holds the mobile menu trigger (`aria-expanded`, `aria-controls`),
  the current area label and the existing `ThemeToggle`. It is not a heading: the
  page owns the single `<h1>`.
* **`MobileNavigation`** is the narrow-screen drawer: a modal `dialog` with a
  scrim, closing on the close button, the scrim, Escape, or selecting a link.
* **`PagePlaceholder`** renders a route's title, purpose and planned work — and
  states plainly that it is structural.

Navigation is defined once in `src/lib/navigation.ts` (`NAV_ITEMS`,
`DEFAULT_ROUTE`, `isActivePath`, `findNavItem`); the sidebar, drawer, header label
and tests all read from it, so a route cannot drift between them. Icons are
inline SVGs in `src/components/icons/NavIcon.tsx` — no icon package.

### Accessibility

* Landmarks: `banner`, `navigation` (named "Primary" and "Mobile"), `main`.
* Skip link to `#main-content`, revealed on focus.
* `aria-current="page"` on the active link in both navigation surfaces.
* Menu trigger exposes `aria-expanded` and `aria-controls`; the drawer has a
  matching `id`, `role="dialog"`, `aria-modal` and an accessible name.
* Focus moves to the drawer's close button on open, returns to the trigger on
  explicit close and on Escape, and is not stolen after link navigation.
* The drawer unmounts when closed, so its links are never reachable while hidden.
* Visible `:focus-visible` rings everywhere, reduced-motion honoured globally,
  and every colour comes from tokens defined in both themes.

## Dependencies (exact pins)

Production: `next 16.4.6`, `react 19.3.1`, `react-dom 19.3.1`.

Development: `typescript 5.9.3`, `@types/node 24.19.2`, `@types/react 19.3.0`,
`@types/react-dom 19.3.0`, `eslint 10.14.0`, `eslint-config-next 16.4.6`,
`vitest 5.1.0`, `vite 8.3.8`, `jsdom 31.0.0`, `@testing-library/react 16.3.3`,
`@testing-library/dom 10.4.2`, `@testing-library/user-event 14.6.9`.

P4.3 added **no** dependency: no icon library, drawer package, router package,
state library, CSS framework or UI kit.

## Structure

```text
frontend/
├── public/{icons,images}/
├── src/
│   ├── app/
│   │   ├── layout.tsx               # <html>, theme bootstrap, globals
│   │   ├── page.tsx                 # redirect → /overview
│   │   ├── loading.tsx              # route loading state
│   │   ├── error.tsx                # sanitized error boundary
│   │   ├── not-found.tsx            # 404 with a way back
│   │   ├── globals.css              # design tokens + base styles
│   │   └── (dashboard)/
│   │       ├── layout.tsx           # renders <AppShell>
│   │       ├── overview/page.tsx
│   │       ├── employees/page.tsx
│   │       ├── attendance/page.tsx
│   │       └── system-status/page.tsx
│   ├── components/
│   │   ├── icons/NavIcon.tsx
│   │   ├── layout/{AppShell,Sidebar,Header,MobileNavigation,PagePlaceholder}
│   │   │   + matching .module.css
│   │   └── ui/{Button,ThemeToggle} + matching .module.css
│   ├── hooks/useTheme.ts
│   ├── lib/{navigation.ts,theme.ts,themeStore.ts}
│   ├── styles/                      # reserved for shared sheets
│   └── types/                       # reserved for API contract types (P4.8)
├── tests/
├── eslint.config.mjs
├── next.config.ts
├── tsconfig.json
├── vitest.config.mts
└── package.json
```

## Design tokens

Tokens live in `src/app/globals.css` and are consumed by CSS Modules; no
component hard-codes a colour.

* Colour: `--color-bg`, `-bg-raised`, `-surface`, `-surface-hover`, `-border`,
  `-border-strong`, `-text`, `-text-muted`, `-text-subtle`, `-accent` (teal),
  `-accent-hover`, `-accent-contrast`, `-focus`, `-success`, `-warning`,
  `-danger`, `-overlay`.
* Spacing `--space-1…8`; radii `--radius-sm|md|lg|pill`; typography
  `--font-sans|mono`, `--text-xs…3xl`, leading/weights; depth
  `--shadow-sm|md|focus`; motion `--transition-fast|base`; `--z-header`.

Every colour token is declared in both `:root, [data-theme="dark"]` and
`[data-theme="light"]`; a test fails if either block loses one.

## Theme implementation

1. **Pre-paint bootstrap** (`THEME_INIT_SCRIPT`, injected in `layout.tsx`):
   applies the stored or system theme to `<html>` before first paint — no flash.
2. **External store** (`src/lib/themeStore.ts`): the document is the source of
   truth; OS changes are followed only while the preference is `system`.
3. **Hook** (`src/hooks/useTheme.ts`): `useSyncExternalStore` with a `null`
   server snapshot, so there is no hydration mismatch and no effect-driven
   `setState`.

## Tests

```bash
npm run test
```

P4.3: **85 tests in 7 suites** (P4.2: 56).

| Suite | Covers |
|---|---|
| `theme.test.ts` | theme primitives, precedence, storage failure, bootstrap script |
| `theme-toggle.test.tsx` | mount application, SSR safety, switching, persistence, keyboard |
| `design-tokens.test.ts` | every colour token in both themes, focus ring, reduced motion |
| `navigation.test.ts` | the four areas, hrefs, default route, active-path rules |
| `app-shell.test.tsx` | skip link, landmarks, all four links, active state, header label, theme inside the shell |
| `mobile-navigation.test.tsx` | collapsed start, `aria-expanded`/`aria-controls`, focus move, Escape, close button, scrim, link navigation, full keyboard path |
| `routes.test.tsx` | root redirect, all four placeholder routes, no fabricated data |

Measured on 2026-11-11 (Node v24.21.0, npm 11.19.0):

| Check | Result |
|---|---|
| `npm ls --depth=0` | exit 0 — 15 packages, unchanged |
| `npm run lint` | exit 0, 0 problems |
| `npm run typecheck` | exit 0 |
| `npm run test` | exit 0, **85 passed / 85**, 7 suites, 21.9 s |
| `npm run build` | exit 0 — static `/`, `/_not-found`, `/attendance`, `/employees`, `/overview`, `/system-status` |

## Fixture policy

No fixture data exists — none is needed for the shell. When fixtures arrive
(P4.4+), they must be development-only, labelled `FIXTURE DATA`, never mixed with
a real response, and never written to the backend or journal.

## Known limitations

* The four areas are **structural placeholders**: no employee CRUD, attendance
  table, checkout control, metrics or health checks yet.
* No API client, no Route Handler proxy, no backend connectivity.
* No live backend verification — the FastAPI startup gate needs PostgreSQL.
* Responsive behaviour is implemented in CSS but was verified by component tests
  and static review only; no browser was driven at 360/768/1280 px.
* `tests/mobile-navigation.test.tsx` asserts the desktop tab order, because jsdom
  does not apply the sidebar's media query.
* `eslint-config-next@16.4.6` bundles plugins whose peers stop at ESLint 9; npm
  resolved them itself (warnings only) and lint passes on ESLint 10.14.0.
* No authentication or RBAC (Phase 5); the backend stays loopback-only with CORS
  disabled.

## Safety

This frontend reads no credential, holds no secret and never contacts the
database. `BACKEND_BASE_URL` arrives as a server-only variable in P4.8; no
`NEXT_PUBLIC_` value may carry a backend URL or secret.
