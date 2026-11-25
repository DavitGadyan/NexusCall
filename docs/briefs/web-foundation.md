# Brief: web app — BFF, security, shells, platform admin and tenant admin consoles

**Path you own:** `apps/web` (not `src/lib/nav.ts`: put nav changes in your report).
**Read:** build.md §3.1 A, §5.1, §8.2, §18, §20, §21, §22; ADR-0006; `contracts/openapi/tenant_service.json` and `services/tenant_service/tenant_service/api/*.py` (the API you render); the existing `apps/web/src/**`.
**Setup:** `pnpm install`. Then `pnpm --filter @nexuscall/web typecheck`, `lint`, `test` and `build`. All four must pass.

Environment variables:

| Variable | Meaning |
|---|---|
| `ENV` | environment name |
| `NEXUS_PUBLIC_BASE_URL` | public URL of the app |
| `NEXUS_OIDC_PUBLIC_ISSUER` | issuer as the browser sees it |
| `NEXUS_OIDC_INTERNAL_BASE` | back-channel realm URL for token, JWKS and logout |
| `KC_WEB_BFF_CLIENT_SECRET` | secret of the `web-bff` client |
| `NEXUS_SESSION_KEY` | 64 hex characters |
| `REDIS_URL` | session store |
| `NEXUS_API_INTERNAL_URL` | internal gateway; the BFF forwards `/api/v1/*` to it |

## BFF (route handlers; tokens never reach the browser)
- `GET /api/auth/login`:
  - Authorization Code with PKCE (S256), state and nonce.
  - `?returnTo=` must be a same-origin path. Guard against open redirects.
  - `?stepup=1` adds `acr_values=aal2&max_age=0`.
- `GET /api/auth/callback`:
  - Exchange the code at the internal URL.
  - Verify the ID token with `jose`: issuer, audience, nonce.
  - Create the session.
- `POST /api/auth/logout`: CSRF-protected. Revoke at Keycloak `end_session`, then destroy the session.
- Session:
  - An opaque random id in a cookie. In production the cookie is `__Host-nx_sid` with HttpOnly, Secure, SameSite=Lax. In development it is `nx_sid` without Secure.
  - Session data is encrypted with AES-256-GCM in Redis, with a TTL.
  - Refresh-token rotation behind a single-flight lock.
  - Idle and absolute timeouts.
- CSRF: double-submit. A readable cookie plus the `X-CSRF-Token` header, bound to the session. Required on every mutating BFF request.
- `/api/bff/[...path]`:
  - Forward only `api/v1/**` to `NEXUS_API_INTERNAL_URL`.
  - Add `Authorization`, `X-Tenant-ID` (from the session, validated against `/api/v1/me`) and `X-Request-ID`.
  - Strip cookies and hop-by-hop headers.
  - Stream SSE through.
  - Map a 401 `step_up_required` to a response the client turns into the step-up login.
- `POST /api/session/tenant` switches the tenant. It must be one of the user's tenants.
- Middleware:
  - CSP with a nonce: no inline scripts, `frame-ancestors 'none'`.
  - Redirect unauthenticated users.
  - Never cache authenticated pages.
- Server-side data helper for RSC, and a small client fetch wrapper. The wrapper handles CSRF, problem+json and step-up.

## UI
Keep the existing tokens and primitives, and extend them: Dialog, Select, Tabs, Toast (aria-live), DataTable with cursor pagination, Form field with errors, ConfirmDialog with typed confirmation, StepUpGate. Radix primitives are allowed.

The UI must be accessible, keyboard-friendly, responsive and dense. Every page has loading, empty and error states. The UI reflects permissions, but the backend decides.

- **Invitation acceptance** `/invite/accept?token=`: preview → sign in (or "set up my account", which calls onboard) → accept → land in the tenant.
- **Tenant switcher**, and a "no workspace" state.
- **Tenant admin**:
  - Team and access: members, a role editor that honours `grantable`, deactivate, version conflicts.
  - Invitations: invite, bulk CSV with a downloadable result report, resend, revoke.
  - Settings for every section. Show field errors from 422 responses and handle version conflicts. Show locked controls as locked, with the reason.
  - Support-access grants.
  - Audit log viewer.
- **Platform admin**:
  - Overview.
  - Tenants: list, create wizard with owner invite, detail, suspend / reactivate / archive with a reason.
  - Users: list, detail with MFA and sessions, deactivate, revoke sessions, effective-permissions viewer.
  - Service accounts.
  - Roles and permissions: catalogue, create a custom role.
  - Privileged requests (the dual-approval queue).
  - Auth events, IdP status.
  - RLS coverage: run a scan, results by service and table with status badges and role issues. Read-only; nothing here edits policy.
  - Break-glass: request, approve, revoke.
  - Platform settings for every section, kill switches, feature flags.
  - Platform audit.
- Do **not** build module pages: CRM, campaigns, calls, inbox, bookings, agents, analytics, integrations. Others add them next.

These APIs are being built in parallel. Code against these shapes:
- Audit, tenant: `GET /api/v1/audit/events?action=&actor_id=&resource_type=&resource_id=&outcome=&from=&to=&limit=&cursor=` → `{items:[{id,occurred_at,actor_type,actor_id,action,resource_type,resource_id,outcome,request_id,correlation_id,producer,metadata}], next_cursor}`.
- Audit, platform: `GET /api/v1/platform/audit/events` (the same, plus `tenant_id`).
- Notifications: `GET /api/v1/notifications?unread=&limit=&cursor=` → `{items:[{id,title,body,link,kind,created_at,read_at}], next_cursor, unread_count}`. `POST /api/v1/notifications/{id}/read`. `POST /api/v1/notifications/read-all`. SSE `GET /api/v1/notifications/stream` with `data:` lines `{type:"notification",id}` or `{type:"ui",event,data}`.

Build a header notification bell on these, and a small `useLiveEvents` hook.

## Tests
- Vitest unit tests: session crypto, CSRF, PKCE, the returnTo guard, the proxy path allowlist and header stripping, cookie flags per environment, permission gating.
- Playwright specs in `apps/web/e2e/`: login with MFA, tenant switch, invite and accept, platform creates a tenant, RLS scan page, a tenant admin is blocked from `/platform`.
  - The dev users and the TOTP secret are in `scripts/gen_keycloak_realm.py`. Use `otplib`.
  - The password comes from the env variable `KC_DEV_USER_PASSWORD`.
  - The specs need a running stack, and you must not start one. Write them carefully, and state in your report that they were not executed.

Commit with the attribution lines from `_common.md`. For report section 6, list the nav entries and the compose env you need.
