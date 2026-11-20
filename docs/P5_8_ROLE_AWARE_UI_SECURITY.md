# P5.8 — Role-aware UI and frontend security hardening

Milestone: **P5.8 — role-aware UI and frontend security hardening**
Repository: `21Oli/AI-Powered-Visual-Attendance-System-v2`
Status: **implemented, awaiting independent review and owner acceptance**
Date: 2026-11-20
Baseline: `main` at `246a2f67f550c99971c947c025cfd78117343649` (P5.7, working tree clean)

This milestone makes the dashboard *reflect* the existing backend authorization
policy: it hides controls a role cannot use, shows one consistent access-denied
screen instead of a page of failed requests, and distinguishes an ended session
from a forbidden action and from a service outage. It adds **no** authorization
mechanism. `src/authz.py` remains the only place that decides what a role may do,
and every protected request is still authorized there.

---

## 1. Objective and scope

In scope:

1. A single presentation-layer permission model that mirrors `src/authz.py`.
2. One React entry point (`useAuthorization`) so pages never re-implement a
   permission check.
3. Role-aware navigation, page guards and action controls for the areas and
   actions that already exist.
4. Consistent 401 / 403 / 503 / network handling in the client.
5. Tests for the full four-role matrix, role-dependent visibility, the guard
   screens and the error paths.
6. A review of the frontend for authorization weaknesses.

Out of scope, deliberately: new backend endpoints, widening the API proxy
allowlist, a second cookie or token store, server-side route protection,
Docker/deployment work, and any change to the computer-vision or
attendance-recording architecture.

## 2. The existing RBAC model (unchanged)

Backend authority: `src/authz.py`.

* `Permission` — eight members: `employees:read`, `employees:create`,
  `employees:update`, `employees:deactivate`, `attendance:read`,
  `attendance:checkout`, `audit:read`, `users:manage`.
* `ROLE_PERMISSIONS` — one immutable table, four roles, no inheritance, no
  wildcard.
* `has_permission()` — fail-closed for unknown roles, unknown permissions and
  inactive principals.
* Every protected route declares exactly one permission through
  `permission_dependency`; an unauthenticated request is `401
  AUTHENTICATION_REQUIRED`, an authenticated one without the permission is a
  sanitized `403 PERMISSION_DENIED`.

| Permission | Admin | HR Manager | Operator | Auditor |
|---|---:|---:|---:|---:|
| employees:read | yes | yes | yes | yes |
| employees:create | yes | yes | no | no |
| employees:update | yes | yes | no | no |
| employees:deactivate | yes | no | no | no |
| attendance:read | yes | yes | yes | yes |
| attendance:checkout | yes | yes | yes | no |
| audit:read | yes | no | no | yes |
| users:manage | yes | no | no | no |

No backend file was modified by this milestone.

## 3. Centralized frontend permission handling

```
src/lib/authz/permissions.ts       the mirrored matrix + fail-closed lookups
src/lib/auth/useAuthorization.ts   useAuthorization(): { ready, role, can }
src/components/auth/RequirePermission.tsx   page guard
src/lib/auth/session-events.ts     one-way "401 was returned" notification
tests/helpers/auth.tsx             role fixtures for component tests
```

`src/lib/authz/permissions.ts` is the **only** frontend place that knows the
matrix. It exports `PERMISSIONS`, `ROLE_PERMISSIONS`, `permissionsFor(role)`,
`roleHasPermission(role, permission)` and `isPermission(value)`. Its rules match
the backend's own:

* the role comes from the P5.7 `/api/v1/auth/me` identity — never from a form
  field, query parameter, header, cookie or storage;
* a role that is not one of the four approved strings holds **nothing**
  (`null`, `"admin"`, `"Superuser"`, an object, an array);
* a permission that is not one of the eight known names is denied;
* there is no wildcard, no hierarchy and no default grant.

`useAuthorization()` reads the P5.7 auth state and returns
`{ ready, role, can(permission) }`. `ready` is `true` only for an authenticated
session with a resolved identity, and `can()` returns `false` while not ready —
so a privileged control is never offered before `/me` answers. Outside an
`AuthProvider` the hook also answers "not ready", so the fail-closed behaviour is
the default rather than an exception.

**These helpers are not enforcement.** `docs/P5_7_FRONTEND_AUTHENTICATION.md` §6
already noted that a client check cannot be one; this milestone keeps that
property and adds the 403 path (§6 below) for the case where the UI and the
policy disagree.

## 4. Role-aware navigation and actions

### Navigation

Every entry in `src/lib/navigation.ts` now carries the permission its data
requires:

| Area | Permission |
| --- | --- |
| `/overview` | `attendance:read` |
| `/employees` | `employees:read` |
| `/attendance` | `attendance:read` |
| `/system-status` | `null` (any signed-in role) |

`visibleNavItems(role)` filters that table, and both the desktop sidebar and the
mobile drawer read it — the same single source of truth as before, so the two
navigation surfaces cannot diverge. An unresolved or unknown role sees only the
entries with no permission requirement.

**Consequence worth stating plainly:** because *every* role holds
`employees:read` and `attendance:read`, all four roles currently see all four
areas. No entry is hidden for any of the four approved roles today. The
filtering is real and tested (unknown role, unresolved session), and it will
matter as soon as an area requires a permission that not every role holds.

### Actions

| Control | Where | Shown to |
| --- | --- | --- |
| Create employee button + form | `EmployeesView`, `CreateEmployeeForm` | Admin, HR Manager |
| Rename (name field, **Save name**) | `EmployeeDetail` | Admin, HR Manager |
| Deactivate (row action, detail button, confirm dialog) | `EmployeeTable`, `EmployeeDetail`, `EmployeesView` | Admin |
| Operator check-out panel | `AttendanceView` → `AttendanceCheckout` | Admin, HR Manager, Operator |
| Audit area | — | not implemented (`audit:read` guards no endpoint, see §7) |
| User management | — | not implemented (see §7) |

Hidden, not merely disabled: an unauthorized role does not receive the control
at all. The one exception kept from P5.4-era UI is the *disabled* state for an
employee id the proxy cannot address — that is a data-shape constraint, not a
role constraint, and it applies to every role equally.

Three layers guard a privileged mutation:

1. the control is not rendered;
2. the handler re-checks the permission before sending (`CreateEmployeeForm`,
   `EmployeeDetail.submitRename`, `EmployeesView.confirmDeactivate`,
   `AttendanceCheckout.confirm`) and reports `ACCESS_DENIED_MESSAGE` instead of
   calling the API;
3. the backend still refuses with `403`, which the UI handles (§6).

A read-only role sees the record with the plain note "Your role can view this
record but not change it." rather than an empty panel.

## 5. Protected page behaviour

`RequirePermission` wraps the page body of `/overview` (`attendance:read`),
`/employees` (`employees:read`) and `/attendance` (`attendance:read`). It:

* renders a status screen while the identity is unresolved — never the page;
* renders one `AccessDenied` screen for a role without the permission;
* renders the page unchanged for a permitted role.

`AccessDenied` is deliberately distinct from a session failure: the heading is
"Access denied", the text is the fixed `ACCESS_DENIED_MESSAGE`, a note says the
session is still active, and the only action is "Back to overview". No retry is
offered, because a forbidden request is not fixed by repeating it.

Because the session cookie is `Path=/api/v1` (P5.7), the role is not known when
the HTML is produced. The prerendered document for `/employees` was inspected
after the production build: it contains the pending screen text once and **0**
occurrences of `Create employee`, `Deactivate` or `employee_id`. Privileged
markup and data are therefore absent from the pre-auth document, not merely
hidden with CSS.

## 6. 401, 403 and service errors

| Outcome | Client behaviour |
| --- | --- |
| `401` on `/api/v1/auth/me` | provider becomes `unauthenticated`; `RequireAuth` redirects once to `/login?next=<validated path>` (P5.7 behaviour, unchanged) |
| `401` on a **data** request | `src/lib/api/client.ts` calls `notifyUnauthorized()`; the provider clears the signed-in UI (`unauthenticated`, `notice: "expired"` when this tab had a session) and the same single redirect happens |
| `403` | session kept intact, no redirect, fixed `ACCESS_DENIED_MESSAGE` in the form/panel/check-out notice; **no automatic retry** |
| `503` / `502` / `504` | sanitized "temporarily unavailable" text and a user-initiated retry; never treated as an ended session |
| network failure | sanitized "could not reach its API" text; never treated as an ended session |
| uncertain mutation (`503 AUDIT_RESULT_UNCERTAIN`, `PERSISTENCE_UNCONFIRMED`) | outcome reported as uncertain, nothing claimed, no automatic retry |

Mechanics:

* the notification travels through `src/lib/auth/session-events.ts`, a
  subscriber set with no payload, so the transport stays React-free and no error
  detail crosses the boundary;
* auth-endpoint calls are excluded (`direct: true` in the transport), because
  the provider owns their 401 outcome — no double transition, no loop;
* `RequireAuth` redirects only when `status === "unauthenticated"`, and the
  login page redirects only when authenticated, so the two conditions remain
  mutually exclusive;
* `isForbidden` / `isUnauthorized` / `isUnavailable` in `src/lib/api/errors.ts`
  are the only classifiers, so a page cannot accidentally call a 401
  "unavailable" or a 403 "expired";
* the check-out classifier gained a `forbidden` kind that states nothing was
  written, offers no refresh and no retry.

## 7. User-management integration status — deferred, and why

`admin`-**only** user administration exists on the backend (`src/api_user.py`,
P5.6) but this milestone did **not** build a UI for it, for two independent
reasons:

1. **The proxy allowlist does not expose it.** `src/lib/server/proxy.ts`
   contains exactly the eight business routes plus `login`, `me` and `logout`.
   There is no `/api/v1/users` rule, so those four endpoints are unreachable
   from the browser today. A user-management UI would require editing that
   allowlist to add `GET /users`, `POST /users`, `PATCH /users/{id}/role` and
   `POST /users/{id}/deactivate`, including new decisions about body limits and
   `Origin` forwarding for `PATCH`. This milestone's instructions freeze the
   allowlist ("existing proxy allowlists remain intact", "no open proxy or new
   unrestricted API route"), and a new bespoke BFF route would be exactly that
   new surface. The allowlist is unchanged and `tests/proxy.test.ts` still pins
   it.
2. **P5.6's database verification is still BLOCKED.** The real-PostgreSQL
   concurrency suite cannot run because the dedicated test database has no
   `app_user`/`user_session` tables (migrations were deliberately not executed).
   A UI presented as verified would be a claim the milestone cannot support.

The `users:manage` permission is modelled (Admin only) and tested, so the
`RequirePermission`/`useAuthorization` wiring is ready when the allowlist
decision is authorized. **Next step if the owner wants it:** a separate
authorized change to `proxy.ts` plus a P5.6 database verification run; it should
not be smuggled into the security-hardening diff.

Likewise there is **no Audit page**: `audit:read` guards no backend endpoint
(the P5.5 verifier is an offline CLI, and the plan states there is no audit read
API). The plan's §10.2 wording — an audit view "only if and when a P5.5 API
design is approved" — is therefore respected by not inventing one. Adding a nav
entry to an empty page would have been a fake capability.

## 8. Security considerations

* **Single authority.** No backend permission logic was copied into the client
  as a *decision*; the mirror only decides what to offer. Every mutation is
  still refused with `403` if the UI is wrong.
* **No second credential store.** No token, role or authorization flag is
  written to `localStorage`, `sessionStorage`, IndexedDB or a script-readable
  cookie. The role lives in the existing in-memory auth state only.
* **No client-controlled role.** Role and permission values are read from the
  `/me` identity and from the compiled matrix; no request body, query or header
  can name a role or permission, and nothing sends one.
* **Proxy untouched.** `src/lib/server/proxy.ts` and
  `src/app/api/v1/auth/[action]/route.ts` are byte-identical to P5.7 (confirmed
  by `git status`). No new API route was added.
* **No privileged content before auth.** The production/prerendered document
  contains only the pending screen (see §5).
* **No unsafe redirects.** All redirect targets still come from
  `safeReturnPath()`; the 401 path reuses it unchanged, and `AccessDenied` links
  to the constant `DEFAULT_ROUTE`.
* **Error sanitation.** Every new message is a local constant; backend text,
  stack traces, hosts, usernames and paths are never rendered.
* **No automatic retries.** Nothing retries an auth, authorization or mutation
  request; the only retry controls are the explicit buttons, and a 403 offers
  none.
* **No logging of identity.** Nothing in the new code logs a role, user, cookie
  or token.

## 9. Tests and exact results

New test files (4) — **71 tests**:

| File | Tests | Covers |
| --- | ---: | --- |
| `tests/permissions.test.ts` | 40 | the full 4×8 matrix (one test per role/permission pair), Admin = all eight, every permission held by at least one role, unknown/missing/non-string roles, unknown permissions, idempotent lookups |
| `tests/role-navigation.test.tsx` | 14 | the four readable areas for all four roles, active-route marking, no gated entry while unresolved or without a provider, `visibleNavItems` fail-closed cases, `RequirePermission` allow/deny/pending/unknown-role and the access-denied screen |
| `tests/role-actions.test.tsx` | 10 | create/deactivate per role, read-only roles still see the directory, empty-state wording, a create refused outside a permitted role (no request), read-only detail panel for Auditor, rename-but-no-deactivate for HR Manager, check-out panel for Admin/HR/Operator and not Auditor |
| `tests/authz-errors.test.tsx` | 7 | 401 clears the UI and redirects with a safe `next`, `/me` 401 redirect, 403 keeps the session and shows the denial with no retry, 403 read panel without retry, 503 not treated as an expiry, network failure not treated as an expiry, uncertain create not retried |

Updated test files (8) — the existing suites now state the role they render
with (`withRole("Admin" | "Operator", …)`) instead of relying on a permissive
default: `app-shell`, `mobile-navigation`, `routes`, `employees-create`,
`employees-deactivate`, `employees-directory`, `employees-rename`,
`attendance-checkout-ui`. Their assertions are unchanged apart from that
wrapper, and they were not weakened: 59 of them initially failed because the
gating is fail-closed, which is the behaviour being introduced.

Commands and results (all run from `frontend/` on 2026-11-20):

| Command | Result |
| --- | --- |
| `npx vitest run` | **614 passed, 0 failed**, 42 files |
| `npm run lint` | exit 0, no output |
| `npm run typecheck` | exit 0, no output |
| `npm run build` | exit 0; routes unchanged (`ƒ /login`, `ƒ /overview`, `ƒ /attendance`, `○ /employees`, `ƒ /system-status`, both API routes) |
| `git diff --check` | exit 0 |

## 10. Known limitations

1. **No page is exclusive to a role subset today.** Every role holds
   `employees:read` and `attendance:read`, so the page guard never denies a
   legitimate visitor. It is proven with `audit:read` (which no page uses) and
   with an unknown role. This is a property of the current matrix, not a gap in
   the wiring.
2. **Hiding is not enforcement.** The backend `403` is the real boundary; the UI
   checks only decide what is offered. A tampered client can still send any
   request and will be refused server-side.
3. **No server-side page protection.** Unchanged from P5.7: the session cookie's
   `Path=/api/v1` means a dashboard request carries no session, so the guard is
   client-side. A visitor with JavaScript disabled sees the pending screen.
4. **No user-management or audit UI** (§7), so `users:manage` and `audit:read`
   are modelled and tested but not exercised by any screen.
5. **Verification is offline.** Every test mocks `fetch`; no live backend,
   browser, camera or deployment was used, so no real `403` was observed end to
   end and no session was expired in a real cookie jar.
6. **The mirrored matrix can drift.** It is pinned by tests against the written
   table, but nothing compiles `src/authz.py` into the frontend. A backend change
   must update `permissions.test.ts` in the same review.
7. **The 401 broadcast is process-local UI state.** It reports what a request
   was told; if the backend is unreachable the client cannot learn the session
   ended, and correctly stays in the "unverified" state instead of guessing.

## 11. Features not verified against a live backend

* A real `403 PERMISSION_DENIED` from FastAPI for each of the four roles
  (employee create/update/deactivate, check-out).
* Session expiry mid-page against a live session store (the 401 path is
  exercised against a mocked response only).
* The user-administration endpoints (`/api/v1/users`) — not integrated, so not
  verified from the frontend at all.
* Any behaviour that requires `ATTENDANCE_TRUSTED_ORIGINS` and a running
  uvicorn instance.

## 12. Review checklist

* `git status` before this milestone: clean at `246a2f6` (P5.7 committed).
* 30 files modified (28 frontend, 2 documents) and 10 new entries (this
  document, 4 source files, the `src/lib/authz/` directory, 4 test files and the
  `tests/helpers/` fixtures); no staged, committed or pushed changes.
* `frontend/src/lib/server/proxy.ts` and the auth BFF route untouched.
* No `.env`, secret, credential, migration or database was read or executed.
* No attendance, employee, audit, biometric or embedding data was accessed;
  every test uses inline fixtures.
* P5.9 has not been started.
