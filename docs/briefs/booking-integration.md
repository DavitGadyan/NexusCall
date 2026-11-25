# Brief: booking-service, integration-service

**Paths you own:** `services/booking_service`, `services/integration_service`, `services/fake_providers/fake_providers/routers/{google_calendar,hubspot}.py`.
**Databases / app roles:** `booking` / `booking_app`, `integration` / `integration_app`.
**build.md:** §3.1 J, M; §5.4; §19 (all); §22 items 10–16; §14 Phases 4 and 7.

## booking-service
- Tables:
  - `resources`: bookable calendars or staff, with timezone and an optional calendar connection.
  - `availability_rules`: weekly hours, slot length, buffers.
  - `appointments`: contact, resource, time range, status tentative|confirmed|canceled|completed|no_show, source, external event id, version.
  - `calendar_sync_state`
- **No double booking.** Add a PostgreSQL exclusion constraint: `EXCLUDE USING gist (tenant_id WITH =, resource_id WITH =, tstzrange(start_at, end_at) WITH &&) WHERE (status IN ('tentative','confirmed'))`. Translate a violation into 409.
- Tentative bookings expire. A job cancels them after N minutes.
- Internal API exactly as in conventions §5: availability, tentative (IK), confirm (IK), cancel (IK).
  - Availability subtracts existing appointments, and the external calendar's busy times when one is connected.
  - Tentative and confirm verify that the booking belongs to the `contact_id` given in the source or context.
- Public API (`bookings:read` / `bookings:manage`): list and calendar range, create, reschedule, cancel, resources, availability rules. Create, reschedule and cancel take an Idempotency-Key and are audited.
- Calendar adapter (`nexus_providers.calendar.CalendarProvider`):
  - Real: Google Calendar REST (freeBusy, events insert and delete) through httpx. The OAuth access token is refreshed from the refresh token, which comes from `CredentialResolver.resolve(tenant, "google_calendar")`.
  - Fake: the simulator.
  - External writes are idempotent, using a deterministic event id. Reconcile after an ambiguous failure.
- Events: `booking.created`, `booking.confirmed`, `booking.canceled`. Reminders go out as `notification.requested`.
- Seed: one resource with weekday 9–17 rules per demo tenant.
- Tests:
  - Concurrent tentative bookings for one slot: exactly one succeeds.
  - Idempotent create.
  - Ownership checks.
  - Expiry.
  - Reschedule conflicts.
  - Cross-tenant isolation and permissions.

## integration-service (the only writer of tenant secrets in Vault)
- Tables:
  - `provider_connections`: tenant-owned. Provider, scope, status active|disabled|revoked|error, `vault_ref`, masked account identifier, last_validated_at, last_rotated_at, next_rotation_due, last_vault_status. **Never a secret value.**
  - `oauth_states`
  - `webhook_endpoints`: url, hashed/encrypted secret reference in Vault, event types, status.
  - `webhook_deliveries`
  - `inbound_webhook_registry`
- Connections API (`integrations:read` / `integrations:manage`; create, rotate and revoke need step-up and are audited):
  - `POST /api/v1/integrations/connections`:
    1. Validate the provider's field shape.
    2. Write to Vault at `tenant_secret_path(env, tenant, provider, connection_id)`.
    3. Store metadata only.
    4. Run a server-side connection test through the provider adapter (in fake mode, the simulator).
    5. Return the status.

    The response never contains the credential.
  - `GET` (list and detail): masked metadata, rotation and health.
  - `POST /{id}/test`
  - `POST /{id}/rotate`: write the new version, validate it, then switch. Keep the previous version until the validation succeeds.
  - `POST /{id}/disable` and `/revoke`: revoke deletes the Vault secret and emits `integration.connection.changed`.
- OAuth, authorization-code, for `google_calendar` and `hubspot`:
  - `GET /api/v1/integrations/oauth/{provider}/start`: state stored server-side, with PKCE where supported.
  - `GET /oauth/callback`: exchange, then put the refresh token in Vault.
  - Show the granted scopes and the connected account.
- Internal: `GET /internal/v1/connections/resolve?provider=`, as in conventions §5.
- Platform (`platform:secrets:read` / `platform:secrets:manage`, `platform:integrations:*`):
  - `GET /api/v1/platform/secrets`: secret references with owning service, environment, rotation status, last successful retrieval, health. Never values.
  - Platform-level provider connections at `platform_secret_path`.
  - Rotation reminders, as a periodic job that sends a notification.
- Outbound webhooks:
  - Endpoints CRUD (`integrations:manage`). The URL is validated with `validate_outbound_url`, at registration and again at every delivery.
  - Deliveries on the `integrations.webhooks` queue:
    - signed with HMAC (`nexus_core.webhooks.sign_payload`) using a secret kept in Vault
    - no redirects, timeouts, bounded retries with backoff and jitter
    - a delivery log, and `webhook.delivery.failed` when retries run out
  - Consume the domain events to deliver: `lead.created`, `call.completed`, `booking.*`, `email.reply.sent`.
- Tests (§22):
  - The credential is written to Vault and never returned. Mock Vault over HTTP with respx.
  - The DB rows hold references only.
  - No secret appears in logs. Capture the logs and assert.
  - Rotation rolls back when validation fails.
  - A revoked connection is no longer resolvable.
  - An SSRF corpus for webhook URLs.
  - Signature and replay.
  - OAuth state checks: CSRF, and state reuse.
  - Cross-tenant isolation and permissions.
