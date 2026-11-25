# Common brief for web module agents (workspace and admin pages)

You add product pages to the existing Next.js app in `apps/web`. The foundation is already built: sign-in and session (BFF), the API proxy, security middleware, app shells, UI primitives, and the platform and tenant admin consoles.

## Read first
1. `docs/briefs/_common.md`: rules, commit format, report format. Its Python steps do not apply to you.
2. This file, then your own brief.
3. `build.md` §8.2, §18, §21 (information architecture) and the "Definition of UI completeness" in §23.
4. The existing app. Learn its conventions before you write anything:
   - `apps/web/src/lib/` (server data helper, client fetch wrapper, permissions, nav)
   - `apps/web/src/components/ui/` (primitives)
   - one finished page group, for example the tenant admin "Team & access" and "Invitations" pages
5. The API contracts you render: `contracts/openapi/<service>.json`. Where a contract is unclear, read the router source in `services/<pkg>/<pkg>/api/`.

## Rules
- Create files **only** under the route folders and component folders your brief lists.
- Do not edit:
  - shared primitives (`components/ui`)
  - `src/lib/**`
  - the BFF and middleware
  - other modules' pages
  - `src/lib/nav.ts` (the links already exist)

  If you need a change there, describe it exactly in your report.
- Put module-specific components under `apps/web/src/components/<your-module>/`.
- Every page needs:
  - loading, empty and error states
  - permission-aware actions. Hide or disable what the user cannot do. The backend still decides.
  - field errors from 422 responses
  - optimistic-lock conflicts (409) handled with a reload prompt
  - step-up sign-in when the API answers `step_up_required`
- Mutations go through the client fetch wrapper. It adds the CSRF token and an `Idempotency-Key` where the API requires one.
- Never render untrusted HTML directly. Email bodies and similar content go through the existing sanitizer and a sandboxed frame.
- Never show secrets. Credential forms are write-only.
- Lists use cursor pagination (`items`, `next_cursor`).
- Live updates use the existing `useLiveEvents` hook.
- The UI must be accessible, keyboard-friendly, responsive and dense. Keep the existing design tokens.
- Charts: invoke the `dataviz` skill before you write any chart.

## Done when
- `pnpm --filter @nexuscall/web typecheck`, `lint`, `test` and `build` all pass.
- You have added Vitest tests for non-trivial logic (formatters, form schemas, state reducers).
- You have added Playwright specs for your module's main flows under `apps/web/e2e/<your-module>/`. They need a running stack, which you must not start. State in your report that they were not executed.
- You have committed on your branch with the attribution lines from `_common.md`.
