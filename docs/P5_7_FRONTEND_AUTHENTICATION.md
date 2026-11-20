# P5.7 — Frontend authentication

Milestone: **P5.7 — frontend authentication and session integration**
Repository: `21Oli/AI-Powered-Visual-Attendance-System-v2`
Status: **implemented, awaiting review and owner acceptance**
Date: 2026-11-20

This milestone adds a login page, session restoration, protected dashboard
routes, sign-out and the surrounding tests to the existing Next.js dashboard. It
does **not** add an authentication mechanism: the FastAPI backend, its session
service, its cookie policy and its CSRF rule are used exactly as delivered in
P5.4.

---

## 1. Backend contract (as read from source, not assumed)

| Route | Method | Auth input | Success | Failures |
| --- | --- | --- | --- | --- |
| `/api/v1/auth/login` | `POST` | JSON `{ "username": string, "password": string }` (`extra="forbid"`), and **exactly one trusted `Origin` header** | `200` `{ "user": { user_id, username, role, is_active } }` + `Set-Cookie: va_session=<token>` | `401 AUTHENTICATION_REQUIRED` (unknown user, wrong password and inactive account are indistinguishable), `403 ORIGIN_NOT_ALLOWED`, `429` rate-limited, `503 DATABASE_UNAVAILABLE` |
| `/api/v1/auth/me` | `GET` | the `va_session` cookie only | `200` `{ user_id, username, role, is_active }` with `Cache-Control: no-store` | `401 AUTHENTICATION_REQUIRED` (absent/empty/malformed/unknown/revoked/expired/inactive), `503`, `500` |
| `/api/v1/auth/logout` | `POST` | the `va_session` cookie, and the same trusted-`Origin` guard | `204` no body + cookie-clearing `Set-Cookie` | `403 ORIGIN_NOT_ALLOWED`; `503`/`500` **without** the clearing cookie (revocation unconfirmed) |

Cookie attributes (from `src/api_cookies.py` / `src/config.py`):

```
name      va_session
Path      /api/v1          (API namespace only; host-only, no Domain)
HttpOnly  true             (JavaScript cannot read it)
Secure    false in dev/test, true in staging/production
SameSite  Lax
Max-Age   43200 seconds (12 h absolute cap, not a sliding idle timeout)
```

The raw token exists only as the `Set-Cookie` value. No JSON body contains it,
and the backend stores only its digest.

**Consequence that shapes this milestone:** because `Path=/api/v1`, the browser
does not send `va_session` to `/overview`, `/login` or any other dashboard URL.
A Next.js middleware or server component therefore cannot see the session. The
frontend must ask `/api/v1/auth/me` (a path the cookie *is* sent to) — which is
what it does. Widening the cookie path or adding a second cookie to make
middleware possible would have weakened the approved policy, so neither was
done.

## 2. Frontend architecture

```
src/lib/auth/paths.ts          safe redirect targets (open-redirect refusal)
src/lib/auth/messages.ts       sanitized user-facing failure text
src/lib/auth/AuthProvider.tsx  the single auth state model (client)
src/components/auth/LoginForm.tsx        /login form
src/components/auth/RequireAuth.tsx      dashboard route guard
src/components/auth/AuthScreens.tsx      pending + verification-failure screens
src/components/auth/UserMenu.tsx         identity + sign out in the shell header
src/app/login/page.tsx         the /login route (outside the dashboard group)
src/app/layout.tsx             mounts AuthProvider once for the whole tree
src/app/(dashboard)/layout.tsx wraps the shell in RequireAuth
```

Existing pieces reused unchanged: `src/lib/api/auth.ts` (`login`,
`fetchCurrentUser`, `logout`), `src/lib/api/client.ts`, the BFF route
`src/app/api/v1/auth/[action]/route.ts`, the proxy policy
`src/lib/server/proxy.ts`, `Button`, `TextField`, `StatusMessage`, the
navigation table and the theme tokens.

### State model

| State | Meaning | UI |
| --- | --- | --- |
| `loading` | `/me` in flight (initial mount or retry) | status screen only; protected content is not mounted |
| `authenticated` | `/me` returned the four-field identity | dashboard renders; login page redirects away |
| `unauthenticated` | `/me` answered `401`, or sign-out succeeded | redirect to `/login?next=…` (or show the form, on `/login`); the expiry notice appears only when this tab had held a session |
| `error` | network, `502/503/504`, or malformed body | "Session check failed" + **Try again** |

A 401 is the *only* authentication-negative signal. A network failure or a 5xx
is an unknown state, never "signed out": the guard shows a retry screen and
explicitly says the user has not been signed out.

### Flow

```
Browser                    Next.js (same origin)          FastAPI
  │                              │                          │
  │ GET /overview                │                          │
  │─────────────────────────────>│  (guard mounts, no cookie sent to this path)
  │                              │ GET /api/v1/auth/me      │
  │                              │─────────────────────────>│ cookie: va_session
  │                              │<─────────────────────────│ 200 user / 401
  │  render page, or 303 to      │                          │
  │  /login?next=%2Foverview     │                          │
  │                              │                          │
  │ POST /api/v1/auth/login      │                          │
  │  {username,password}         │  Origin forwarded as-is  │
  │─────────────────────────────>│─────────────────────────>│ 200 + Set-Cookie
  │<── Set-Cookie: va_session ───│<─────────────────────────│
  │  (HttpOnly; JS never sees it)│                          │
  │                              │                          │
  │ POST /api/v1/auth/logout     │  Origin + va_session     │
  │─────────────────────────────>│─────────────────────────>│ 204 + clearing cookie
```

1. **Login** — `LoginForm` validates both fields locally, posts the pair to the
   existing endpoint, and on success stores the returned identity in the auth
   context. The session itself is the cookie the browser just received.
2. **Restoration** — on every full page load the provider issues exactly one
   `GET /api/v1/auth/me` (guarded against duplicate in-flight calls, which also
   covers StrictMode's double effect). A refresh therefore restores the session
   with no user action.
3. **Protection** — `RequireAuth` renders children only when authenticated. In
   `loading` it renders the status screen, so protected content is never
   flashed; in `error` it renders the retry screen.
4. **Logout** — `UserMenu` calls logout; the client state is cleared **only**
   after the backend confirms revocation (`204`). If logout fails, the user
   stays signed in and sees a fixed failure message, because the cookie is still
   live.
5. **Expiry/revocation** — the next `/me` answers `401`, the provider becomes
   `unauthenticated`. The login page explains an expiry **only when this tab
   already held an authenticated session** (`notice: "expired"`); a first visit
   or a signed-out visitor gets the neutral copy, because a cold `401` cannot
   distinguish "never signed in" from "cookie since expired" — the backend
   answers both identically on purpose.

### Redirect safety

Every redirect target comes from `safeReturnPath()`:

* must start with a single `/` (so `//host` and absolute URLs are refused);
* no backslashes, control characters or `.`/`..` segments (raw or `%2e`);
* the pathname must belong to an approved dashboard area in `NAV_ITEMS`, so
  `/login`, `/api/...` and unknown paths fall back to `/overview`;
* the fragment is dropped, the query is kept (employee paging).

The login page redirects *away* only when authenticated, and the guard
redirects *to* the login page only when unauthenticated — mutually exclusive
conditions, so a redirect loop is not reachable.

### Cookie and CSRF behaviour

* The frontend never reads, copies or stores the token; there is no
  `localStorage`/`sessionStorage`/IndexedDB entry and no script-readable cookie.
* `fetch` uses `credentials: "same-origin"`; the client never fabricates an
  `Origin` header, so the value the backend checks is the one the browser
  produced.
* The proxy forwards `Origin` for login/logout only, the `va_session` cookie for
  `/me` and logout only, and the backend's `Set-Cookie` for login/logout only.
  No other browser header is forwarded.

## 3. API proxy

`src/lib/server/proxy.ts` is unchanged: the allowlist still contains exactly the
eight business routes plus `login`, `me` and `logout`, with per-route method
matching, no wildcard, no client-supplied upstream URL, no redirect following,
no CORS headers, an 8 s timeout, a 4 KiB body cap and sanitized error
envelopes. The backend origin stays server-only in `BACKEND_BASE_URL`; no
`NEXT_PUBLIC_*` variable was added.

## 4. Environment variables

| Variable | Where | Purpose |
| --- | --- | --- |
| `BACKEND_BASE_URL` | frontend server only | absolute `http(s)` origin of FastAPI; never exposed to the browser |
| `ATTENDANCE_TRUSTED_ORIGINS` | backend deployment | the exact origins allowed to call login/logout; an empty list rejects every login |

No new variable was introduced by this milestone.

## 5. Local development

```bash
# 1. backend (repository root), with its own database configuration
python -m uvicorn src.api:app --port 8000

# 2. dashboard
cd frontend
npm install
BACKEND_BASE_URL=http://127.0.0.1:8000 npm run dev     # http://localhost:3000/login
```

The backend must include `http://localhost:3000` in its trusted origins for
sign-in to work locally, and `Secure` must be off for plain-HTTP development
(the default in the development environment). With no session, `/overview`
redirects to `/login?next=%2Foverview`.

Checks used for this milestone:

```bash
cd frontend
npm run test        # vitest run
npm run lint        # eslint .
npm run typecheck   # tsc --noEmit
npm run build       # next build
```

## 6. Security considerations

* **Single authority.** The backend decides who is authenticated and what they
  may do. The role shown in the header is display data; P5.7 added no
  client-side authorization decision. P5.8 adds a presentation-only permission
  mirror for hiding controls — see `docs/P5_8_ROLE_AWARE_UI_SECURITY.md` — which
  never replaces backend enforcement.
* **No token in JavaScript.** The session exists only as an HttpOnly cookie; the
  auth state contains identity fields and callbacks, and a test pins that shape.
* **Error sanitation.** User-facing text is chosen locally from the failure
  class (`network`, `401`, `403`, `429`, `5xx`, malformed); backend messages,
  driver text, hosts and usernames are never rendered.
* **Duplicate submissions.** A re-entrancy guard plus a disabled button, so a
  double click or a held Enter exchanges credentials once.
* **No automatic retries.** Nothing retries login, logout or `/me`
  automatically; the only retry is the user pressing **Try again** on the
  verification-failure screen. Ambiguous mutations are therefore never replayed.
* **Open redirects.** Refused by construction (see above), with tests for
  absolute URLs, protocol-relative URLs, traversal and `/login` itself.
* **CSRF.** Unchanged: login and logout still require the backend's
  exactly-one-trusted-`Origin` guard, which the proxy forwards verbatim.
* **No password logging.** Nothing in the new code logs; the password lives in
  component state for the duration of the request only.

## 7. Known limitations

1. **No server-side route protection is possible with the approved cookie.**
   `Path=/api/v1` means the cookie is not sent to dashboard routes, so the guard
   is client-side by design. A visitor with JavaScript disabled sees the
   "Checking your session…" screen rather than the dashboard; the API still
   refuses every unauthenticated request, so no data is exposed.
2. **One `/me` request per page load.** Client-side navigation between dashboard
   pages does not re-check the session (the provider is mounted at the root);
   an expiry is detected on the next full load, on the next `/me`, or when an
   API call returns 401. Data screens do not yet translate a 401 into a
   sign-in redirect — that belongs with P5.8's 401/403 handling.
3. **Verification is offline only.** Every test mocks `fetch`; no live backend,
   browser, camera or production deployment was exercised for this milestone.
   Cookie application is therefore asserted at the header level (the BFF
   preserves `Set-Cookie` with `HttpOnly`, `Path=/api/v1`, `SameSite=lax`) rather
   than observed in a real cookie jar.
4. **The expiry notice does not survive a reload.** It is derived from state
   held in this tab, not from a stored flag, so a visitor who reloads after an
   expiry sees the neutral copy. Distinguishing the two cases across loads would
   need either a client-side marker or a backend signal that separates "no
   session" from "expired session" — neither was added, because the backend's
   single `401` is deliberate and no second client-side store is wanted.
5. **The login page is dynamic** (`force-dynamic`) because it reads
   `?next=` from the request; it is not statically prerendered.
