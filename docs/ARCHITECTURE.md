# NexusCall — Architecture

> Status: Phase 0 baseline. Source requirements: [`build.md`](../build.md). Decisions: [`docs/adr/`](adr/).
> Companion docs: [SERVICE_MAP.md](SERVICE_MAP.md), [THREAT_MODEL.md](THREAT_MODEL.md), [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## 1. System context (C4 level 1)

```text
                         ┌───────────────────────────────┐
  Agency operators ─────▶│                               │──▶ Twilio (PSTN voice, Media Streams)
  Tenant users     ─────▶│          NexusCall            │──▶ Amazon SES (outbound + inbound email, SNS events)
  Leads / customers ────▶│  multi-tenant AI call center  │──▶ Anthropic Claude (LLM, via AI Gateway)
   (phone + email)       │                               │──▶ Deepgram (STT) / ElevenLabs (TTS)
                         │                               │──▶ Google / Microsoft calendars, CRMs (OAuth)
                         └───────────────────────────────┘──▶ Tenant webhook endpoints (signed)
```

Actors are described in build.md §1.1. A user can belong to several tenants and hold a different role in each ([ADR-0006](adr/0006-iam-keycloak-bff.md)).

## 2. Containers (C4 level 2)

```text
Browser ──HTTPS──▶ Traefik (api-gateway) ──▶ web (Next.js UI + BFF) ──▶ Traefik internal routes ──▶ services
                       │                                                          
Twilio / SES ─HTTPS──▶ Traefik /webhooks/* ──▶ call-service | email-service | email-inbox-service | integration-service
Twilio Media ─WSS────▶ Traefik /media/*    ──▶ voice-service

   ┌───────────────────────── internal network (not publicly exposed) ─────────────────────────┐
   │ tenant  crm  campaign  call  voice  ai-agent  ai-gateway  mcp-registry  tool-execution     │
   │ booking  email  email-inbox  integration  notification  analytics  audit                   │
   │ workers: calls, voice-post, email-send, email-inbound, ai-drafts, integrations, imports,   │
   │          analytics, notifications, outbox-relay(s), celery-beat (single, locked)           │
   │ data:    postgres(+pgvector)  rabbitmq  redis  minio  vault  keycloak  clamav              │
   │ obs:     otel-collector  prometheus  grafana  loki  tempo                                  │
   │ dev-only: fake-providers, mailpit                                                          │
   └────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Edge
- **web**: a single Next.js 15 app (TypeScript, Tailwind, shadcn/ui) with three route groups:
  - `(workspace)`: the tenant user experience
  - `(tenant-admin)`: tenant settings, team and policies
  - `(platform-admin)`: the agency control plane, build.md §18 and §21
- **BFF**: Next.js route handlers run the OIDC Authorization Code + PKCE flow against Keycloak. Access and refresh tokens stay server-side in an encrypted session store (Redis). The browser only holds an `HttpOnly; Secure; SameSite=Lax` session cookie. Mutating requests require a CSRF double-submit token. The BFF forwards the user's access token to services. It never sends privileged service credentials ([ADR-0006](adr/0006-iam-keycloak-bff.md)).
- **api-gateway (Traefik)** handles:
  - TLS
  - host and path routing
  - per-IP and per-route rate limits
  - body size limits
  - `X-Request-ID` injection
  - strict CORS (web origin only)
  - a separate router for `/webhooks/*` and `/media/*` with no user-auth middleware

  Traefik is a coarse filter only. Authorization happens in each service ([ADR-0010](adr/0010-api-gateway-traefik.md)).

### 2.2 Application services
There are 16 FastAPI services. Each one has its own:
- PostgreSQL database
- migrations (Alembic)
- DB roles
- Dockerfile
- OpenAPI contract
- health endpoints (`/healthz` for liveness, `/readyz` for DB, broker and Vault reachability)
- tests

[SERVICE_MAP.md](SERVICE_MAP.md) lists each service's data ownership and dependencies.

### 2.3 Shared packages (infrastructure only)
| Package | Contents |
|---|---|
| `packages/nexus_core` | Settings (pydantic-settings), JSON logging with redaction, OpenTelemetry setup, JWT/JWKS verifier, `require_permission()` dependency, `tenant_session()` (transaction + `SET LOCAL`), outbox/inbox mixins + relay, idempotency store, Vault client, RFC 7807 error shape, health router, fake-provider production guard |
| `packages/event_schemas` | Versioned Pydantic `EventEnvelope` + payload models; exported JSON Schema committed under `contracts/events/` |
| `packages/provider_contracts` | `Protocol`s for Telephony, Email, LLM, STT, TTS, Calendar, CRM adapters + reusable contract test suites |

No ORM models or domain logic are shared across services (build.md §13).

## 3. Key cross-cutting mechanisms

### 3.1 Tenant context and RLS ([ADR-0007](adr/0007-database-per-service-rls.md))
1. A request arrives with a user JWT. `nexus_core.auth` verifies the signature against the JWKS and checks `iss`, `aud`, `exp`, `nbf` and the algorithm allowlist.
2. `X-Tenant-ID` selects the active tenant. The service resolves membership and permissions through tenant-service's `/internal/v1/authz/check` call, cached for 30 s in Redis. The cache key is `(sub, tenant, permission-version)`, and a membership change bumps the version so stale entries are skipped.
3. `tenant_session(tenant_id, actor)` opens a transaction and runs `SET LOCAL app.tenant_id`, `app.actor_id` and `app.actor_type`. All queries run inside it.
4. Every tenant table has `tenant_id uuid NOT NULL`, `ENABLE` and `FORCE ROW LEVEL SECURITY`, and policies for SELECT, INSERT, UPDATE and DELETE. Missing context makes `current_setting(..., true)` return NULL, so no rows match.
5. Roles per service DB:

   | Role | Used by | Privileges |
   |---|---|---|
   | `<svc>_owner` | migrations only | owns tables |
   | `<svc>_app` | runtime | DML only; not owner, not superuser, not BYPASSRLS |
   | `<svc>_inspector` | RLS dashboard | catalog read only |
   | `<svc>_platform` | break-glass endpoints | separate audited path; sees only rows granted by an active support grant through a policy on `app.support_grant_id` |

6. Workers rebuild the same context from the job's `tenant_id` and actor in every transaction.

### 3.2 Reliable messaging ([ADR-0008](adr/0008-outbox-inbox-messaging.md), [ADR-0011](adr/0011-celery-rabbitmq-topology.md))
- A business write and its `outbox` row commit in the same transaction. Each service's `outbox-relay` publishes to RabbitMQ with publisher confirms, then marks the row sent.
- Consumers insert `(consumer, event_id)` into an `inbox` table, which has a unique constraint, before applying side effects. A duplicate event becomes a no-op.
- Celery tasks carry only IDs and references: never message bodies, never secrets.
- RabbitMQ uses quorum queues, a dead-letter exchange per queue, manual acks with `acks_late`, and bounded prefetch.

### 3.3 Idempotency
`Idempotency-Key` header on create/launch/send/book endpoints. The key is stored per `(tenant_id, operation, key)` under a unique constraint, together with `request_hash`, the SHA-256 of canonical JSON. States are `processing`, `succeeded` and `failed_retryable`.

| Situation | Response |
|---|---|
| Same key, different hash | 422 |
| Same key while still `processing` | 409 |
| Same key after success | replay of the stored response |

External providers get the same key where they support one: Twilio `X-Twilio-Idempotency-Token` and SES client tokens.

### 3.4 Secrets ([ADR-0009](adr/0009-vault-secrets.md))
- Vault KV v2 path: `secret/data/{env}/tenants/{tenant_id}/{provider}/{connection_id}`.
- Platform credentials live under `secret/data/{env}/platform/...`.
- integration-service is the only writer for tenant paths. Other services and workers have read-only policies scoped to the providers they use.
- Databases store a `vault_ref` and metadata only.
- Locally, Vault runs in dev mode with AppRole per service and a bootstrap script. In Kubernetes, each service uses the Vault Agent Injector with the Kubernetes auth method.

### 3.5 AI Gateway boundary ([ADR-0012](adr/0012-ai-gateway-boundary.md))
```text
caller service ─▶ ai-gateway ─▶ (policy: tenant allowlist, classification, budget, kill switch) ─▶ Anthropic adapter
                      │
                      └─ proposed tool_use ─▶ tool-execution ─▶ mcp-registry (is tool registered, granted, risk class?)
                                                   │            ─▶ owning service API (resource ownership check)
                                                   │            ─▶ Vault (credential injected here, never to the model)
                                                   └─ Class ≥3 → ai_approval_requests (human approval) before execution
```
- Untrusted content (emails, transcripts, RAG chunks, tool outputs) goes to the model as trust-labelled data blocks, never as part of the system prompt.
- Model output is schema-validated before anything acts on it.
- Default models are listed below. They are configurable per tenant and agent within the platform allowlist.

  | Use | Model |
  |---|---|
  | Voice turn-taking | `claude-haiku-5-5` |
  | Drafts and summaries | `claude-sonnet-5-5` |
  | Offline evaluations | `claude-opus-5-5` |

## 4. Key flows

### 4.1 Outbound call campaign
```text
UI launch ─▶ campaign-service (Idempotency-Key, perms campaigns:launch, audit) ─▶ snapshot audience from crm (suppression/DNC/consent filtered)
  ─▶ outbox campaign.launched ─▶ worker-calls (calls.schedule): per-contact call rows in call-service, respecting window/tz/concurrency
  ─▶ calls.dispatch: pre-dial re-check DNC/window ─▶ Twilio create call (idempotency token) ─▶ state initiating
  ─▶ Twilio status webhooks (signature verified, deduped) ─▶ ringing/connected ─▶ TwiML <Connect><Stream> to voice-service
  ─▶ voice-service: Deepgram ⇄ ai-gateway(Claude) ⇄ ElevenLabs; tools via tool-execution (e.g. booking.create_tentative)
  ─▶ call ends ─▶ voice.postprocess: transcript+recording to MinIO, summary/qualification, outcome ─▶ events to analytics/crm
```
After an ambiguous Twilio timeout, the worker fetches the call by idempotency token or `ProviderCallSid` before retrying anything (build.md §3.1G).

### 4.2 Inbound call
1. Twilio's voice webhook arrives at `/webhooks/twilio/voice` on call-service and its signature is verified.
2. The `To` number is looked up in `inbound_numbers`. That is trusted configuration and yields the tenant and agent. Unknown numbers get a polite rejection.
3. call-service creates the call record (direction `inbound`) and returns TwiML: disclosure prompt, then `<Connect><Stream>` to voice-service.
4. From there the session follows the same pipeline as outbound. Handoff to a human uses a Twilio conference with a tenant queue.

### 4.3 Inbound email → AI draft → approved send
1. SES receives the message, stores it in S3, and publishes an SNS notification to email-inbox-service.
2. email-inbox-service verifies the SNS signature, dedupes on SNS message ID and email `Message-ID`, and resolves the mailbox from the recipient domain/address mapping. That mapping is trusted configuration.
3. It persists the message and threads it. Attachments get a ClamAV scan.
4. `emails.ai_draft` calls ai-gateway for classify + summary + draft. RAG runs with the tenant filter applied in the query. The draft row is created with provenance, then the review queue is notified.
5. A reviewer approves the draft at version N with an Idempotency-Key. The server re-checks `emails:drafts:approve` and tenant ownership, freezes version N, creates a `send_command`, and audits the approval.
6. `emails.send_approved` sends exactly the frozen content through SES, setting `In-Reply-To` and `References`. The SES message ID is persisted.

The send endpoint accepts only a `draft_id` and `version`, never free-form recipients or body.

## 5. Observability
- **Logs:** structured JSON through `nexus_core.logging`, with `service`, `env`, `request_id`, `correlation_id`, `tenant_id`, `actor` and `operation`. A redaction filter strips emails, phone numbers, tokens and known secret keys.
- **Tracing:** OTel SDK exporting to otel-collector, then Tempo for traces and Prometheus for metrics. W3C `traceparent` is propagated in AMQP headers.
- **Metrics:** Grafana dashboards are provisioned from `infra/grafana`, covering API RED, queues, Celery and business KPIs.
- **Errors:** Sentry-compatible DSN, optional, with a PII scrubber.
- **LLM telemetry:** ai-gateway records model, latency, tokens and cost. Prompt content is not stored by default.

## 6. Deployment
- **Local:** `docker compose` brings up the full stack, with fakes enabled by `PROVIDER_MODE=fake`. A Mailpit UI is available.
- **Production:** generic Kubernetes ([ADR-0004](adr/0004-deployment-self-hosted-kubernetes.md)):
  - Helm umbrella chart with one subchart per service
  - Migrations run as a Helm pre-upgrade Job under the `<svc>_owner` role
  - NetworkPolicies default-deny
  - Pod Security `restricted`
  - Vault Agent injector
  - Terraform manages cluster add-ons: ingress, cert-manager, operators for Postgres (CloudNativePG), RabbitMQ (cluster operator), Redis, MinIO, Vault, and the observability stack

## 7. Known simplifications (Phase 0 position)
- Locally, one PostgreSQL server hosts 17 databases (16 services plus Keycloak). Production can split them across CloudNativePG clusters by load.
- Billing is out of scope. Usage metering exists so billing can be added later.
- Only one real provider per category at first (Twilio, SES, Anthropic, Deepgram, ElevenLabs, Google Calendar). Others plug in through adapters later.
