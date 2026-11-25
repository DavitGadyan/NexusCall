# NexusCall — Implementation Plan

Source of truth for requirements: [`build.md`](../build.md). Architecture: [ARCHITECTURE.md](ARCHITECTURE.md). This plan is updated at the end of every phase.

## 1. Decisions and assumptions

| # | Question (build.md §16) | Decision | Source | ADR |
|---|---|---|---|---|
| 1 | Deployment target | Self-hosted / generic Kubernetes (Helm). Docker Compose locally | **User** | [0004](adr/0004-deployment-self-hosted-kubernetes.md) |
| 2 | Telephony provider | Twilio (Programmable Voice + Media Streams) | **User** | [0002](adr/0002-initial-providers.md) |
| 3 | Email provider | Amazon SES for outbound and inbound (SES receipt rule → S3 + SNS) | **User** | [0002](adr/0002-initial-providers.md) |
| 4 | Identity provider | Keycloak (OIDC) | Assumption (spec default) | [0006](adr/0006-iam-keycloak-bff.md) |
| 5 | Regions / data hosting | Single region, configured per deployment. Tenant `data_region` field reserved | Assumption | — |
| 6 | Call direction | **Inbound and outbound in the MVP** | **User** | [0003](adr/0003-inbound-and-outbound-calls.md) |
| 7 | AI auto-send email | Off. Every AI draft needs human approval. Auto-send policy is deferred | Assumption (spec default) | [0012](adr/0012-ai-gateway-boundary.md) |
| 8 | Volumes | MVP target: 50 tenants, 200 concurrent calls, 20k calls/day, 100k emails/day, ~2 TB/yr recordings | Assumption | — |
| 9 | Recording retention | 90 days by default, configurable per tenant (7–730 days). Access needs `calls:recordings:read` | Assumption | — |
| 10 | Greenfield? | Yes. The repo held only README + build.md | Verified | — |
| 11 | Service topology | **All bounded contexts are separate services from Phase 1** | **User** | [0005](adr/0005-full-microservice-topology.md) |
| 12 | LLM / STT / TTS | Anthropic Claude via the AI Gateway. Deepgram STT. ElevenLabs TTS | **User** | [0002](adr/0002-initial-providers.md) |

All assumptions can be reversed through configuration or an adapter. None of them is baked into domain logic.

## 2. Phases, deliverables and exit criteria

Every phase delivers migrations, unit and integration tests, OpenAPI plus event JSON Schemas, Docker config, docs and a phase report. The report covers scope, files changed, tests run and their results, security notes, limitations and the next phase.

### Phase 0 — Discovery and planning ✅
- **Deliverables:**
  - ARCHITECTURE.md, SERVICE_MAP.md, THREAT_MODEL.md, this plan
  - ADR-0001…0012
  - risk register (§4)
  - machine-readable architecture graph (`docs/architecture/architecture.graph.json`)
- **Exit:** the user has reviewed the decisions.

### Phase 1 — Platform foundation
1. Workspace: `uv` workspace (`pyproject.toml` at root), `pnpm` workspace for `apps/web`, root `Makefile`, `.editorconfig`, `.gitignore`, `.dockerignore`, `.env.example`.
2. `packages/nexus_core`: settings, logging and redaction, OTel, health router, error model, the JWT verifier (stub key source until Phase 2), `tenant_session()`, outbox/inbox/idempotency SQLAlchemy mixins, Vault client, fake guard.
3. `packages/event_schemas`: the envelope plus a JSON Schema export script. `packages/provider_contracts`: Protocols.
4. A service template copied into 16 services. Each copy has a FastAPI app, `/healthz`, `/readyz`, `/metrics`, an Alembic env, and a Dockerfile (python:3.12-slim pinned by digest, multi-stage, uid 10001, read-only rootfs). Each also has a smoke test.
5. `services/fake_providers`: simulators for Twilio REST, webhooks and media, SES and SNS, Anthropic messages API (deterministic), Deepgram, ElevenLabs and Google Calendar.
6. `docker-compose.yml`: networks `edge` and `internal`. Postgres with `infra/postgres/init/*.sql` creates DBs and the owner/app/inspector roles. Also RabbitMQ (definitions.json with quorum queues and DLX), Redis, MinIO with buckets, Vault dev plus `infra/vault/bootstrap.sh` (policies and AppRoles per service), Keycloak with a realm import, ClamAV, Mailpit and the observability stack.
7. CI (`.github/workflows/ci.yml`): ruff, mypy, pytest per service (matrix), web lint, typecheck and tests, docker build, Trivy, gitleaks.
- **Exit:** `make up` brings everything up healthy, `make test` is green, and CI is green.

### Phase 2 — IAM, tenants, RLS, audit (blocks all business features)
- Keycloak realm `nexus`:
  - clients `web-bff` (confidential, PKCE) and `svc-<name>` (client credentials)
  - realm role `platform_owner`/`platform_operator` only for platform staff
  - MFA required for admin roles
  - `acr` step-up levels
- tenant-service:
  - tenants, memberships, permission catalogue (seeded from build.md §5.2), system roles, custom tenant roles (subsets of the catalogue only)
  - invitations: states per §18.3, hashed tokens, a resend that supersedes the old token, bulk CSV with a report
  - service accounts via the Keycloak admin API
  - support grants
  - `/internal/v1/authz/check`
- RLS on every tenant table. A CI lint uses an Alembic hook to assert RLS and FORCE on any table with `tenant_id`.
- audit-service: append-only with a hash chain. App role grants are INSERT and SELECT only.
- BFF auth in the web app, the tenant switcher, and permission-aware navigation.
- Platform Admin UI:
  - Overview
  - Tenants
  - Users
  - Invitations
  - Service accounts
  - Roles & effective permissions
  - Sessions (revoke)
  - Auth events
  - RLS coverage (scan through `<svc>_inspector`)
  - Secrets health (metadata only)
  - Break-glass
  - Audit viewer
- Tenant Admin UI: Team & Access, Invitations.
- ai-gateway, mcp-registry and tool-execution get auth context, the tenant policy-check skeleton and their audit emitters.
- **Tests:**
  - cross-tenant negative suite (`tests/security/test_tenant_isolation.py`)
  - role-boundary matrix
  - invitation single-use, expiry and enumeration
  - pool context-leak test
  - step-up enforcement
- **Exit:** acceptance criteria §15.1–4 and §22.1–9, 18–20.

### Phase 3 — CRM, campaigns, tenant configuration, agent config
- **CRM:**
  - contacts/leads CRUD with filters
  - CSV import: MinIO → `imports.process`, with per-row validation and dedupe by normalized E.164/email
  - consent records with source, timestamp and policy version
  - suppressions (DNC and email)
  - segments: rule DSL compiled to parameterized SQL
- **Campaign service:**
  - draft → scheduled → running → paused → completed | canceled | failed
  - idempotent launch with snapshot, windows, concurrency caps, tenant kill switch
- Outbox relay → RabbitMQ in production use.
- **Tenant settings UI:** branding, timezone/locale, business hours, modules/flags (bounded by platform), limits, retention, consent policy.
- **ai-agent:**
  - agents and prompt versions
  - knowledge sources: upload → chunk → embed via the gateway → pgvector with an RLS-filtered search
- **mcp-registry:** tool catalogue seeded with internal tools (`crm.get_contact` C1, `booking.find_slots` C0, `booking.create_tentative` C2, `booking.confirm` C3, `call.transfer_to_human` C2, `crm.add_note` C2) and agent-tool grants.
- **UI:** Leads, Contacts, Imports, Segments, Call campaigns builder, AI Agents, Knowledge.

### Phase 4 — Call center (outbound + inbound)
- **Telephony:**
  - Twilio adapter (create call, fetch, hangup, conference, numbers) plus fake
  - signature-verified webhooks
  - call state machine: queued → scheduled → initiating → ringing → connected → in_progress → completed | failed | canceled | opted_out
  - reconcile-before-retry
  - pre-dial gates for DNC, consent, window and caps
- **Inbound:** `inbound_numbers` (number → tenant, agent, business hours, overflow) and the TwiML disclosure + `<Connect><Stream>` response.
- **voice-service:**
  - Media Streams WS (μ-law 8 kHz) → Deepgram streaming with endpointing → turn manager (barge-in cancels TTS) → ai-gateway streaming (`claude-haiku-5-5`) → ElevenLabs streaming `ulaw_8000` → Twilio
  - silence and max-duration timeouts
  - fallback utterances when a provider is down
  - handoff to a human via conference
  - transcript segments and dual-channel recordings in MinIO
- **voice.postprocess:** summary, qualification and outcome as structured output, then `call.outcome.extracted`.
- **booking-service:** availability rules, appointments using `EXCLUDE USING gist (tenant_id WITH =, resource_id WITH =, tstzrange WITH &&)`, a Google Calendar adapter plus fake, and idempotent creation.
- **UI:** live activity (SSE), call history, call detail (transcript, recording via signed URL, outcome), inbound numbers, bookings calendar.
- **E2E:** lead → campaign → call → outcome → booking, and an inbound call that produces a transcript.

### Phase 5 — Email campaigns
- **Senders and domains:** SES identity, DKIM tokens, SPF/DMARC check status.
- **Templates:** versioned and sanitized (bleach), with a link allowlist and blocked `javascript:`/`data:` schemes.
- **Email campaigns:** the state machine from §4.1, segment selection, sending windows, batched fan-out to `emails.campaign_send`, and a token bucket per tenant, domain and provider.
- **Delivery events:** SES events via SNS with verified signatures. Message state follows the recipient state machine from §4.1.
- Auto-pause on bounce/complaint thresholds.
- RFC 8058 one-click unsubscribe plus a preference page, and cross-channel suppression through events.
- **UI:** senders/domains, templates editor with preview, email campaigns, reports.

### Phase 6 — Inbox and AI reply drafts
- SES inbound (SNS → S3 object) normalized into mailboxes, threads and messages. Threading uses `Message-ID`, `In-Reply-To` and `References`.
- **Attachments:** size and type limits, ClamAV, quarantine, and signed URLs with a 5-minute TTL.
- **`emails.ai_draft`:**
  - classify intent and priority, summarize, retrieve (tenant-filtered)
  - draft with `claude-sonnet-5-5`, using trust-labelled blocks and JSON-schema output
  - store `draft_version` with provenance: model, prompt version, sources, quality flags
- **Review queue:** approve, edit (creates a new version), reject, regenerate, assign, escalate.
- **Approve:** step-up check, permission re-check, version freeze, `send_command` (unique on draft and version), outbox entry.
- **Send:** only the frozen version can be sent.
- **Tests:** unapproved draft cannot be sent, stale approval, double approve, prompt-injection corpus.
- **UI:** Inbox threads, Drafts, Approval queue.

### Phase 7 — Integrations, analytics, AI control plane
- OAuth connections (Google Calendar, HubSpot as the first CRM target) with refresh tokens in Vault.
- Provider connection wizard: write to Vault → test → status, with rotate, revoke and disable actions.
- Outbound webhooks: HMAC-SHA256 signed, with timestamp, retries, delivery log and SSRF guard.
- analytics-service: event-fed facts, dashboards, cost tracking for AI, telephony and email, and audited exports.
- Platform UI:
  - AI Gateway: providers, models, routing, allowlists, budgets, kill switches, security events
  - MCP registry and tools
  - policy proposals with dual approval
  - Queues & DLQ with audited replay
  - Services health
  - Usage

### Phase 8 — Production hardening
- Helm umbrella chart:
  - per-service Deployments and HPAs (call/voice on concurrent-session metrics)
  - PDBs
  - NetworkPolicies, default-deny
  - Pod Security restricted
  - Vault Agent annotations
  - migration Jobs
- Terraform for the cluster add-ons listed in ADR-0004.
- k6 load and soak tests (200 concurrent fake calls, 100k emails/day), chaos testing (worker kill, broker failover).
- Backup/restore drills (CloudNativePG PITR, MinIO versioning), Vault rotation drills, break-glass exercise.
- Adversarial AI testing, threat-model review, compliance checklist for counsel.

## 3. Proposed repository layout
```text
apps/web/                       Next.js (workspace, tenant-admin, platform-admin route groups + BFF)
services/<service_name>/        app/, migrations/, tests/, Dockerfile, pyproject.toml   (16 services)
services/fake_providers/        DEV-ONLY simulators
workers/                        thin Celery entrypoints per worker container (import owning service code)
packages/{nexus_core,event_schemas,provider_contracts}/
contracts/{openapi,events}/     generated, committed contract snapshots
infra/{compose,postgres,rabbitmq,vault,keycloak,grafana,prometheus,otel,helm,terraform}/
tests/{e2e,security,load}/
docs/{adr,architecture}/ + ARCHITECTURE.md, IMPLEMENTATION_PLAN.md, THREAT_MODEL.md, SERVICE_MAP.md, RUNBOOK.md
```

## 4. Risk register

| ID | Risk | L | I | Mitigation | Owner phase |
|---|---|---|---|---|---|
| R1 | 16 services plus workers make local dev heavy (RAM ~10–12 GB) | H | M | Compose profiles (`core`, `calls`, `email`, `ai`, `obs`); `make up PROFILE=...` | 1 |
| R2 | Real-time voice latency over budget (target < 1.2 s turn latency) | M | H | Streaming everywhere, Haiku-class model, early TTS on first sentence, colocate voice-service | 4 |
| R3 | RLS context leak via async pool | M | H | `SET LOCAL` only; pool-reset on checkin (`DISCARD ALL`); dedicated test | 2 |
| R4 | Duplicate calls after provider timeouts | M | H | Reconcile-before-retry; idempotency token; unique `(tenant, campaign, contact, attempt_no)` | 4 |
| R5 | SES sandbox / domain verification delays block testing | H | L | Fake SES + Mailpit; documented production-access request | 5 |
| R6 | Prompt injection leads to unauthorised side effects | M | H | Architectural controls (ADR-0012), adversarial corpus | 3–6 |
| R7 | Compliance obligations (TCPA, GDPR, recording consent) vary by jurisdiction | H | H | Configurable controls; legal review flagged; not claimed as compliance | 4–8 |
| R8 | Distributed-transaction complexity across 16 services | M | M | Outbox/inbox, sagas only where needed, contract tests | 3+ |
| R9 | Keycloak admin-API coupling for user lifecycle | L | M | Wrap in an `IdpAdmin` adapter within tenant-service | 2 |
| R10 | Cost runaway (LLM/telephony) | M | M | Budgets, caps, kill switches, alerts | 4–7 |
| R11 | Spec scope is very large; risk of many shallow services | H | M | Vertical slices; each service gets real responsibilities in its phase; skeletons only expose health until then | all |

## 5. Unresolved product decisions (non-blocking, revisit later)
- Billing provider and pricing model. Usage metering is built either way.
- Which CRMs beyond HubSpot.
- Whether calls go to non-US/EU countries, which affects caller-ID and number provisioning.
- SMS notifications: Twilio SMS is an optional adapter in Phase 7.
- Auto-send policy for low-risk email intents. Deferred, off by default.
