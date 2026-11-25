# NexusCall — Service & Dependency Map

Each service owns its database. No service reads or writes another service's database. Cross-context data moves through versioned REST (`/api/v1` public, `/internal/v1` service-to-service with service JWTs) or through RabbitMQ events.

## 1. Services, ownership and justification

| # | Service | DB | Owns (tenant-scoped unless noted) | Public API surface | Why a separate deployable |
|---|---|---|---|---|---|
| 1 | tenant-service | `tenant` | tenants*, memberships, roles*, permission catalogue*, role_permissions*, invitations, teams, brands, tenant_settings, platform_settings*, feature_flags, support_grants, service_accounts (metadata) | `/tenants`, `/memberships`, `/invitations`, `/roles`, `/platform/*` | Security-critical authz source; different change cadence and access controls |
| 2 | crm-service | `crm` | contacts, leads, organizations, tags, custom_field_defs, consent_records, suppressions (DNC + email), imports, segments | `/leads`, `/contacts`, `/imports`, `/segments`, `/suppressions` | Large data volume, import workloads |
| 3 | campaign-service | `campaign` | call_campaigns, audience_snapshots, schedules, campaign_runs, kill_switches | `/campaigns/*` | Scheduling/launch lifecycle independent of dialing |
| 4 | call-service | `call` | calls, call_attempts, provider_events, inbound_numbers, call_outcomes, reconciliation_runs | `/calls/*`, `/inbound-numbers`, `/webhooks/twilio/*` | Latency-sensitive webhooks; state machine |
| 5 | voice-service | `voice` | voice_sessions, transcript_segments (refs), recordings (metadata), handoffs | `/media/twilio` (WSS), `/voice/sessions/*` | Long-lived WebSockets, CPU/network profile, scales on concurrent calls |
| 6 | ai-agent-service | `agent` | agents, prompt_versions, knowledge_sources, knowledge_chunks (pgvector), eval_datasets, eval_runs, agent_runs | `/ai/agents/*`, `/knowledge/*` | Agent workflow state separate from provider policy |
| 7 | ai-gateway | `aigw` | ai_providers*, ai_models*, routing_policies, tenant_model_allowlists, ai_budget_policies, ai_requests, ai_usage_records, ai_security_events, kill_switches | `/ai/responses` (internal), `/platform/ai/*` | Security boundary to model providers |
| 8 | mcp-registry | `mcpreg` | mcp_servers*, mcp_server_versions*, tool_definitions*, tool_permissions, agent_tool_grants, ai_policy_versions, policy_proposals | `/ai/tools`, `/ai/mcp-servers`, `/platform/ai/policies/*` | Control-plane catalogue, approval workflow |
| 9 | tool-execution | `toolexec` | tool_execution_records, ai_approval_requests, idempotency | `/ai/tool-approvals/*` (internal exec API only) | Isolated egress + credential access; strict network policy |
| 10 | booking-service | `booking` | availability_rules, appointments (exclusion constraint), calendar_sync_state | `/bookings/*` | Double-booking guarantees, calendar sync jobs |
| 11 | email-service | `email` | sender_identities, domains, templates(+versions), email_lists, email_campaigns, email_messages, provider_events, quotas, unsubscribe_tokens | `/email/campaigns/*`, `/email/templates`, `/email/senders`, `/webhooks/ses/events`, `/u/{token}` | Bulk send pipeline, deliverability controls |
| 12 | email-inbox-service | `inbox` | mailboxes, threads, messages, attachments (metadata), drafts, draft_versions, review_assignments, send_commands | `/email/inbox/*`, `/email/drafts/*`, `/webhooks/ses/inbound` | Untrusted inbound content handling, HITL workflow |
| 13 | integration-service | `integration` | provider_connections (vault_ref only), oauth_states, webhook_endpoints, webhook_deliveries, inbound_webhook_registry | `/integrations/*`, `/webhooks/endpoints/*` | Only Vault writer; SSRF-guarded egress |
| 14 | notification-service | `notify` | notifications, notification_preferences, notification_templates | `/notifications/*` (+ SSE stream) | Fan-out + SSE connections |
| 15 | analytics-service | `analytics` | event-fed fact tables / read models, exports | `/analytics/*`, `/exports/*` | Read-heavy, aggregation workloads |
| 16 | audit-service | `audit` | audit_events (append-only, hash-chained) | `/audit/events` | Tamper-evident store with restricted privileges |

`*` = platform-scoped (global catalogue) tables. They have no RLS on `tenant_id` and are writable only through `platform:*` permissions.

## 2. Synchronous dependencies (internal REST, service JWT via Keycloak client credentials)

```text
all services ──▶ tenant-service  /internal/v1/authz/check, /internal/v1/tenants/{id}/settings
all services ──▶ audit-service   (via event, not sync — see below)
campaign     ──▶ crm             /internal/v1/segments/{id}/resolve (paged, suppression-filtered)
call         ──▶ crm             /internal/v1/contacts/{id}/contactability  (DNC/consent pre-dial check)
voice        ──▶ ai-gateway      /internal/v1/ai/responses (streaming)
voice        ──▶ ai-agent        /internal/v1/agents/{id}/runtime-config
ai-agent     ──▶ ai-gateway      /internal/v1/ai/responses, /internal/v1/ai/embeddings
ai-gateway   ──▶ tool-execution  /internal/v1/tools/execute (registered tool_id only)
tool-exec    ──▶ mcp-registry    /internal/v1/tools/{id}/grant-check
tool-exec    ──▶ booking | crm | email-inbox | call   (resource-owning APIs)
email-inbox  ──▶ ai-agent        /internal/v1/drafting/runs
email-inbox  ──▶ email-service   /internal/v1/send (approved send_command only)
web (BFF)    ──▶ every service's public /api/v1 via Traefik
```

## 3. Asynchronous events (exchange `nexus.events`, topic routing key = `event_type`)

| Producer | Event types | Consumers |
|---|---|---|
| tenant | `tenant.created/suspended`, `membership.changed`, `invitation.created/accepted/revoked`, `settings.changed` | notification, analytics, all (authz cache bust) |
| crm | `lead.created`, `contact.updated`, `suppression.added`, `import.completed` | campaign, analytics, email |
| campaign | `campaign.launched/paused/resumed/canceled`, `campaign.kill_switch` | call, analytics |
| call | `call.state_changed`, `call.completed`, `call.opted_out` | crm (opt-out → suppression), analytics, notification |
| voice | `voice.session.ended`, `voice.handoff.requested` | call, notification |
| ai-agent | `call.outcome.extracted`, `email.reply_draft.generated` | call, crm, email-inbox |
| booking | `booking.created/confirmed/canceled` | crm, analytics, notification |
| email | `email.message.state_changed`, `email.unsubscribed`, `email.complaint` | crm (suppression), analytics |
| email-inbox | `email.inbound.received`, `email.reply_draft.requested/approved/rejected`, `email.reply.sent` | analytics, notification |
| integration | `integration.connection.changed`, `webhook.delivery.failed` | notification |
| ai-gateway / tool-exec | `ai.request.*`, `ai.tool.*`, `ai.budget.exceeded`, `ai.provider.circuit_opened` | audit, analytics, notification |
| **all** | `audit.event` | audit |

Audit events travel over the outbox, so an audit record cannot be lost when the business write commits. Envelope: build.md §6.3, defined in `packages/event_schemas`.

## 4. Task queues (Celery, RabbitMQ quorum queues + DLX `<queue>.dlq`)

| Queue | Worker container | Owning service code | Concurrency guidance |
|---|---|---|---|
| `calls.schedule`, `calls.dispatch`, `calls.provider_callbacks` | worker-calls | call-service | latency-sensitive, prefetch 1 |
| `voice.postprocess` | worker-voice-post | voice-service | CPU/IO |
| `emails.campaign_send`, `emails.send_approved` | worker-email-send | email-service | rate-limited per tenant/domain/provider |
| `emails.inbound_process` | worker-email-inbound | email-inbox-service | IO |
| `emails.ai_draft` | worker-ai-drafts | email-inbox-service | LLM-bound, budgeted |
| `integrations.webhooks` | worker-integrations | integration-service | egress-guarded |
| `imports.process` | worker-imports | crm-service | bulk |
| `analytics.aggregate` | worker-analytics | analytics-service | bulk, low priority |
| `notifications.send` | worker-notifications | notification-service | IO |
| periodic (reconciliation, quota reset, invitation expiry, rotation reminders) | celery-beat (single, Redis lock) | per-service tasks | — |

## 5. Infrastructure dependencies per service

| Service | Postgres | RabbitMQ | Redis | MinIO | Vault paths (read) |
|---|---|---|---|---|---|
| tenant | ✔ | pub/sub | authz cache | logos | `platform/keycloak-admin` |
| crm | ✔ | pub/sub | — | imports | — |
| campaign | ✔ | pub/sub | locks | — | — |
| call | ✔ | pub/sub | concurrency counters | — | `tenants/*/twilio/*` |
| voice | ✔ | pub | session state | recordings, transcripts | `tenants/*/{twilio,deepgram,elevenlabs}/*`, `platform/{deepgram,elevenlabs}` |
| ai-agent | ✔ (+pgvector) | pub/sub | — | knowledge docs | — |
| ai-gateway | ✔ | pub | rate limits, breaker state | — | `platform/anthropic`, `tenants/*/anthropic/*` |
| mcp-registry | ✔ | pub | — | — | — |
| tool-execution | ✔ | pub | — | — | `tenants/*/{google_calendar,crm_*}/*` |
| booking | ✔ | pub/sub | — | — | via tool-execution / own calendar sync paths |
| email | ✔ | pub/sub | quotas | — | `tenants/*/ses/*`, `platform/ses` |
| email-inbox | ✔ | pub/sub | — | raw mail, attachments | `platform/ses-inbound` |
| integration | ✔ | pub/sub | oauth state | — | **write** `tenants/*/*` |
| notification | ✔ | sub | SSE pub/sub | — | `platform/ses` |
| analytics | ✔ | sub | — | exports | — |
| audit | ✔ | sub | — | archive | — |
