# Brief: crm-service, campaign-service

**Paths you own:** `services/crm_service`, `services/campaign_service`.
**Databases / app roles:** `crm` / `crm_app`, `campaign` / `campaign_app`.
**build.md:** §3.1 E, F; §5.4; §7; §11; §14 Phase 3.

## crm-service
- Tables:
  - `contacts`: names, company, normalized email and `phone_e164` (each tenant-unique when present), timezone, country, locale, status, owner, source, `custom_fields` jsonb validated against definitions, version.
  - `leads`: contact (composite FK), status new|contacted|qualified|unqualified|booked|won|lost, source, attribution ids, owner, score.
  - `organizations`
  - `tags` and `contact_tags`
  - `custom_field_defs`
  - `consent_records`: **append-only**. Channel, status granted|withdrawn, lawful basis, source, captured_at, policy_version, evidence reference.
  - `suppressions`: channel phone|email|all, `value_hash` (sha256 of the normalized value), value, contact, reason, source. Unique per tenant + channel + hash.
  - `contact_notes`
  - `imports` and `import_rows`
  - `segments`: rule DSL as jsonb.
  - `exports`
- Public API:
  - Contacts and leads CRUD with filters, sort and keyset pagination (`leads:read` / `leads:create` / `leads:update`).
  - Tags, custom field definitions, consent endpoints.
  - Suppressions: list and add (`leads:update`). Removal needs step-up and is audited.
  - Segments CRUD with a preview count (`campaigns:read` / `campaigns:create`).
- Imports: `POST /api/v1/imports`, multipart CSV up to 10 MB (`leads:create`).
  - The file goes to the `imports` bucket, then to the `imports.process` task.
  - Validate each row: email syntax, and phone to E.164 with `phonenumbers`.
  - Dedupe by normalized email or phone (update or create).
  - Capture consent metadata.
  - Produce a result report CSV (row, status, error), served through a presigned URL.
  - Emit `import.completed`.
- Exports: `POST /api/v1/leads/exports` (`leads:export`, step-up, audited, async).
- Segment DSL: `{"op":"and|or","rules":[{"field","operator","value"} | nested]}`.
  - Fields and operators come from allowlists.
  - Compile to SQLAlchemy expressions with bind parameters.
  - Enforce depth and size limits.
  - Custom fields are allowed only when the key exists in the definitions.
- Contactability. A contact is not contactable when it is:
  - suppressed for the channel or for `all`
  - latest consent for the channel is `withdrawn`
  - has no address
  - inactive
- Internal API exactly as in conventions §5. Mask email and phone in `GET /internal/v1/contacts/{id}`.
- Consumers:

  | Event | Effect |
  |---|---|
  | `call.opted_out` | phone suppression, consent withdrawn |
  | `email.unsubscribed`, `email.complaint` | email suppression |
  | `booking.created` | lead status `booked` |
  | `call.outcome.extracted` | lead status and qualification |

- Emits: `lead.created`, `contact.updated`, `suppression.added`, `import.completed`.
- Seed, per demo tenant:
  - 40 contacts with ids `demo_id("contact", n)`. acme uses n = 1..40, globex uses n = 101..140.
  - Phones in the reserved +1 555 01xx range, emails `@example.test`, a few suppressed.
  - One segment "All contactable leads", `demo_id("segment", 1)` for acme and `demo_id("segment", 101)` for globex.

## campaign-service
- Tables:
  - `call_campaigns`: name, agent, segment, state, timezone, calling window, max concurrency, calls per minute, max attempts, retry delay, start and end, version.
  - `campaign_contacts`: state pending|in_flight|completed|failed|skipped|retry_wait, attempts, next_attempt_at, last call id, outcome.
  - `campaign_events`
  - `kill_switches`
- State machine: draft → scheduled → running ⇄ paused → completed | canceled | failed. Enforce it with optimistic locking, and unit-test it.
- API:
  - CRUD (`campaigns:read` / `campaigns:create` / `campaigns:manage`).
  - `POST /{id}/schedule`
  - `POST /{id}/launch` (`campaigns:launch`, Idempotency-Key, audited) → 202. Before launching, validate:
    - the agent, through ai-agent `GET /internal/v1/agents/{id}`: kind `voice`, status active
    - the segment, through crm
    - the limits, against the tenant's `communication` settings
    - the kill switches
  - The audience snapshot runs asynchronously on `calls.schedule`. It pages crm `segments/{id}/resolve` (channel `phone`) into `campaign_contacts` with an idempotent upsert.
  - Pause, resume, cancel. Cancel also cancels queued calls through call-service.
  - `GET /{id}/report`
  - Tenant kill switch: `POST /api/v1/campaigns/kill-switch` (`campaigns:manage`, step-up, audited). It stops dispatch immediately and emits `campaign.kill_switch`.
- Dispatcher: a Redis-locked `PeriodicJob` that runs every few seconds. For each running campaign:
  1. Check the tenant's `effective_calls_enabled`, the campaign window, the calls-per-minute token bucket and the in-flight count against max concurrency.
  2. Claim due contacts with `FOR UPDATE SKIP LOCKED`.
  3. Call `POST call-service /internal/v1/calls` with the idempotency key from conventions §5.
  4. Mark the contact `in_flight`.

  If call-service refuses a contact as not contactable, mark it `skipped`. Never dispatch for a paused, canceled or killed campaign.
- Consumer `call.completed`:
  - Update the contact.
  - Schedule a retry for no-answer, busy or failed while attempts remain.
  - Mark opted-out contacts `skipped`.
  - Complete the campaign when nothing is left.
- Internal: the `dispatch-state` endpoint.
- Tests:
  - Concurrent duplicate launches produce one launch.
  - Pacing and concurrency caps hold.
  - The kill switch stops dispatch.
  - Duplicate `call.completed` events do nothing the second time.
  - Cross-tenant isolation and permission boundaries.
