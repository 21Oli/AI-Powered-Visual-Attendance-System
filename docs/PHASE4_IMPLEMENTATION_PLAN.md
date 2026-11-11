# Phase 4 — Implementation plan and milestone status

Owner-approved direction: Next.js App Router, TypeScript (strict), CSS Modules,
CSS custom properties, no Tailwind, no UI framework, dark graphite + teal with a
light theme, and a same-origin Route Handler proxy so FastAPI keeps CORS
disabled.

Baseline when Phase 4 began: branch `main`, HEAD `55f0dae`, clean working tree,
Phase 3 committed and accepted.

---

## 1. Milestones

| # | Milestone | Status |
|---|---|---|
| P4.1 | Repository inspection, architecture and dependency approval list | **PASSED** — `READY FOR OWNER REVIEW` accepted by the owner |
| P4.2 | Frontend scaffold and design system (this document's subject) | **READY FOR OWNER REVIEW** |
| P4.3 | Application shell and navigation (sidebar, responsive drawer, skip link) | not started |
| P4.4 | Overview screen | not started |
| P4.5 | Employees screen (list, create, detail, rename, deactivate) | not started |
| P4.6 | Attendance screen (journal, checkout confirmation) | not started |
| P4.7 | System status screen (API health, offline states) | not started |
| P4.8 | Same-origin Route Handler proxy + live typed client | not started |
| P4.9 | Final verification, documentation and acceptance review | not started |

Every milestone stops for explicit owner approval; no milestone starts early.

---

## 2. Approved dependency set (installed in P4.2)

| Package | Version | Scope |
|---|---|---|
| `next` | 16.4.6 | production |
| `react` | 19.3.1 | production |
| `react-dom` | 19.3.1 | production |
| `typescript` | 5.9.3 | development |
| `@types/node` | 24.19.2 | development |
| `@types/react` | 19.3.0 | development |
| `@types/react-dom` | 19.3.0 | development |
| `eslint` | 10.14.0 | development |
| `eslint-config-next` | 16.4.6 | development |
| `vitest` | 5.1.0 | development |
| `vite` | 8.3.8 | development |
| `jsdom` | 31.0.0 | development |
| `@testing-library/react` | 16.3.3 | development |
| `@testing-library/dom` | 10.4.2 | development |
| `@testing-library/user-event` | 14.6.9 | development |

Pinning rules:

* Exact versions only — no `^`, `~`, `latest` or tags; `package-lock.json` is
  committed and `npm ci` is the reproducible install.
* TypeScript 5.9.3 rather than 7.x: `eslint-config-next@16.4.6` depends on
  `typescript-eslint ^8.56.0`, whose TypeScript 7 support is unverified.
* `@types/node` 24.x matches the runtime (Node v24.21.0).
* `vite` is pinned because Vitest 5 lists it as a **required** peer
  (`^6.4 || ^7 || ^8`); 8.3.8 satisfies it and supports Node 24.
* No package was added without approval; `@eslint/eslintrc` proved unnecessary
  because `eslint-config-next` 16 exports a ready-made flat config array.

Peer-compatibility note: `eslint-config-next@16.4.6` bundles
`eslint-plugin-react@7.37.5` and `eslint-plugin-jsx-a11y@6.10.2`, whose declared
peer ranges stop at ESLint 9. npm resolved these itself as warnings — no
`--force`, no `--legacy-peer-deps`, no suppressed error — and `npm run lint`
passes on ESLint 10.14.0.

---

## 3. Delivered scaffold (P4.2)

```text
frontend/
├── public/{icons,images}/
├── src/
│   ├── app/{layout.tsx,page.tsx,globals.css,loading.tsx,error.tsx}
│   │   plus layout.module.css, page.module.css, loading.module.css, error.module.css
│   ├── components/ui/{Button.tsx,ThemeToggle.tsx + module.css}
│   ├── hooks/useTheme.ts
│   ├── lib/{theme.ts,themeStore.ts}
│   ├── styles/                 # reserved for shared sheets (P4.3)
│   └── types/                  # reserved for API contract types (P4.8)
├── tests/{setup.ts,theme.test.ts,theme-toggle.test.tsx,home-page.test.tsx,design-tokens.test.ts}
├── eslint.config.mjs
├── next.config.ts
├── next-env.d.ts
├── tsconfig.json
├── vitest.config.mts
├── package.json
└── package-lock.json
```

Deliberately absent: sidebar and navigation shell (P4.3), the four screens
(P4.4–P4.7), the API proxy and typed client (P4.8), fixture modules, and any
`NEXT_PUBLIC_` backend value.

---

## 4. Design system

Tokens are declared once in `src/app/globals.css` and consumed only through CSS
Modules.

* Colour: `--color-bg`, `--color-bg-raised`, `--color-surface`,
  `--color-surface-hover`, `--color-border`, `--color-border-strong`,
  `--color-text`, `--color-text-muted`, `--color-text-subtle`,
  `--color-accent`, `--color-accent-hover`, `--color-accent-contrast`,
  `--color-focus`, `--color-success`, `--color-warning`, `--color-danger`,
  `--color-overlay`.
* Spacing: `--space-1` … `--space-8`; radii `--radius-sm|md|lg|pill`.
* Typography: `--font-sans`, `--font-mono`, `--text-xs` … `--text-3xl`,
  `--leading-tight|normal`, `--weight-regular|medium|semibold`.
* Depth and motion: `--shadow-sm|md|focus`, `--transition-fast|base`,
  `--z-header`.

Dark is the CSS default (`:root, [data-theme="dark"]`); `[data-theme="light"]`
overrides every colour, and `tests/design-tokens.test.ts` fails if either block
loses a required token. Accessibility: `:focus-visible` ring on all interactive
elements, reduced-motion support, semantic landmarks, and a sanitized error
boundary that shows only Next's opaque digest.

---

## 5. Theme architecture

| Layer | File | Responsibility |
|---|---|---|
| Bootstrap | `src/lib/theme.ts` (`THEME_INIT_SCRIPT`) | runs synchronously in `<head>`; sets `data-theme` + `color-scheme` before first paint, so there is no flash |
| Store | `src/lib/themeStore.ts` | document is the source of truth; notifies subscribers; follows `prefers-color-scheme` only while the preference is `system`; tolerates storage/`matchMedia` failures |
| Hook | `src/hooks/useTheme.ts` | `useSyncExternalStore` with a `null` server snapshot, so hydration cannot mismatch and no effect-driven `setState` exists |
| Control | `src/components/ui/ThemeToggle.tsx` | accessible button, stable pre-mount label, persisted explicit choice |

Precedence: stored explicit choice → system preference. Storage key:
`attendance-dashboard-theme`; attribute: `data-theme`.

---

## 6. Verification (P4.2)

| Command | Result |
|---|---|
| `npm ci` | exit 0 |
| `npm ls --depth=0` | exit 0 — 15 packages, no unmet peers |
| `npm run lint` | exit 0 — 0 problems, 14 s |
| `npm run typecheck` | exit 0 — 9 s |
| `npm run test` | exit 0 — 56 passed / 56, 4 files, 12.5 s |
| `npm run build` | exit 0 — 22 s, static `/` and `/_not-found` |

No lint rule was disabled, no test weakened and no type escape hatch added. The
one lint finding raised during the milestone (`react-hooks/set-state-in-effect`)
was fixed by replacing effect-driven state with the external-store pattern.

---

## 7. Remaining work and constraints

* Live verification stays blocked until the owner authorizes a running FastAPI
  process and database: the backend's G2+G8 startup gate refuses to start without
  PostgreSQL.
* Fixtures are approved for offline later milestones but must be
  development-only, labelled `FIXTURE DATA`, never mixed with real responses, and
  never written to the backend.
* Attendance has **no server-side pagination** and events carry **no employee
  name** — the attendance screen must paginate client-side and join names from
  `GET /employees`, without inventing either.
* Check-out accepts no request body (`extra: forbid`), so the UI must send none,
  and the 300-second cooldown must be surfaced as-is.
* No authentication or RBAC until Phase 5; the backend stays loopback-only with
  CORS disabled.
