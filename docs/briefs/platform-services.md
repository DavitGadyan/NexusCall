# Brief: audit-service, notification-service, analytics-service

**Paths you own:** `services/audit_service`, `services/notification_service`, `services/analytics_service`.
**Databases / app roles:** `audit` / `audit_app`, `notify` / `notify_app`, `analytics` / `analytics_app`.
**build.md:** §3.1 N, O, P; §8; §10; §18.1 (audit, usage); §20.3.

## audit-service
- Consume `audit.event` from every producer into `audit_events`.
- The table is append-only and hash-chained. There is one chain per tenant, and one "platform" chain for events without a tenant.
  - Serialize appends per chain with `pg_advisory_xact_lock`.
  - Each row stores `chain_seq`, `prev_hash` and `hash = sha256(prev_hash || canonical_json(event fields))`.
  - Use `apply_append_only_rls(..., allow_null_tenant=True)`.
  - Dedupe on `event_id`.
- Strip metadata keys listed in `nexus_core.logging.SENSITIVE_KEYS`, and cap the metadata size.
- Tenant API (`audit:read`):
  - `GET /api/v1/audit/events` with filters `action` (prefix), `actor_id`, `resource_type`, `resource_id`, `outcome`, `from`, `to`, plus keyset pagination.
  - Items are `{id, occurred_at, actor_type, actor_id, action, resource_type, resource_id, outcome, request_id, correlation_id, producer, metadata}`.
  - `POST /api/v1/audit/exports` (step-up, audited): CSV to the `exports` bucket, returned as a short presigned URL.
- Platform API (`platform:audit:read`):
  - `GET /api/v1/platform/audit/events`: the same filters plus `tenant_id`. It includes platform-level events.
  - Cross-tenant reads use the dedicated read-only `audit_platform` DB role (`settings.platform_database_url`). Give it its own `FOR SELECT TO audit_platform` policy.
  - Every platform search writes an audit record of the search itself: who, which filters, how many results.
  - `POST /api/v1/platform/audit/verify` recomputes a chain and reports the first broken link.
- Tests:
  - UPDATE and DELETE are blocked, including for the owner (trigger).
  - `verify` detects a tampered copy.
  - Duplicate events are idempotent.
  - Cross-tenant isolation.
  - The platform path is audited.

## notification-service
- Tables:
  - `notifications` (tenant-owned; `user_id` or `audience_permission`; title, body, link, kind)
  - `notification_reads`
  - `notification_preferences`
  - `email_log` (no bodies: template, recipient hash, status, provider id, error code)
- Internal API as in conventions §5.
- Email templates are code-defined: `invitation`, `support_access_requested`, `support_access_active`, `support_access_rejected`, `draft_ready`, `handoff_requested`, `generic`.
  - Escape all variables.
  - Send both text and HTML.
  - Link variables must start with `settings.public_base_url`.
- Email adapter (`nexus_providers.email.EmailProvider`):
  - Real: Amazon SES v2 through aiobotocore, with platform credentials from `platform_secret_path(env, "notification-service/ses")`.
  - Fake: SMTP to Mailpit (`MAILPIT_SMTP_HOST`, default `mailpit:1025`), so developers can open invitation links.
  - This is operational mail only. Marketing mail belongs to email-service.
- Public API:
  - `GET /api/v1/notifications?unread=&limit=&cursor=` → `{items:[{id,title,body,link,kind,created_at,read_at}], next_cursor, unread_count}`. It returns rows addressed to the caller, or to a permission the caller holds.
  - `POST /api/v1/notifications/{id}/read`
  - `POST /api/v1/notifications/read-all`
  - `GET` / `PUT /api/v1/notifications/preferences`
  - `GET /api/v1/notifications/stream` (SSE):
    - Subscribe to `nexus_core.realtime.ui_channel(tenant)`.
    - Forward `{type:"ui", event, data}` only when the caller holds the event's `permission`.
    - Forward `{type:"notification", id}` for new notifications.
    - Send heartbeats, and close cleanly on disconnect.
- Consumers:

  | Event | Audience |
  |---|---|
  | `notification.requested` | as the payload says |
  | `email.reply_draft.generated` | `emails:drafts:review` |
  | `voice.handoff.requested` | `calls:manage` |
  | `webhook.delivery.failed` | `integrations:manage` |
  | `ai.budget.exceeded` | `tenant:manage` |
  | `import.completed` | `leads:create` |

- Tests:
  - The permission filter on the SSE relay.
  - Template escaping and link validation.
  - The caller allowlist.
  - Cross-tenant isolation.

## analytics-service
- Event-fed read models, tenant-owned with RLS:
  - fact tables for calls, emails, bookings, AI usage, drafts and leads
  - `daily_metrics(tenant_id, day, metric, dimension, value)`, upserted idempotently
- Consumers, with `allowed_producers`: `call.state_changed`, `call.completed`, `call.opted_out`, `call.outcome.extracted`, `booking.*`, `email.message.state_changed`, `email.unsubscribed`, `email.complaint`, `email.reply_draft.*`, `email.reply.sent`, `ai.request.completed`, `lead.created`, `campaign.launched`.
- API (`analytics:read`):
  - `GET /api/v1/analytics/overview?from&to`:
    - call funnel, answer rate and average duration
    - bookings
    - email funnel
    - AI draft approved / edited / rejected rates
    - cost per call, per email and per AI interaction
  - `GET /api/v1/analytics/timeseries?metric=&from=&to=`
  - `GET /api/v1/analytics/campaigns/{campaign_id}`
- Exports:
  - `POST /api/v1/exports` (`analytics:export`, step-up, Idempotency-Key, audited). It creates a job row, the `analytics.aggregate` queue builds the CSV, and the file lands in the `exports` bucket.
  - `GET /api/v1/exports`
  - `GET /api/v1/exports/{id}` returns a short presigned URL and is audited.
- Platform (`platform:usage:read`): `GET /api/v1/platform/usage?from&to` gives usage and cost totals per tenant. It reads through the `analytics_platform` role with an explicit SELECT policy, and it is audited.
- Do not build metrics on opens or clicks. They are unreliable.
