# NexusCall — Build Conventions (binding for every contributor and coding agent)

Read with: `build.md` (requirements), `docs/ARCHITECTURE.md`, `docs/SERVICE_MAP.md`, `docs/THREAT_MODEL.md`, `docs/adr/`.

**The reference implementation is `services/tenant_service`** (tables, migration `0002`, routers, tests). Copy its patterns. Where this document asks for more than tenant-service does (response models, pagination), follow this document.

## 1. Service layout (one bounded context per service)

```text
services/<pkg>/
  <pkg>/settings.py      Settings(ServiceSettings): env-driven, no secrets in code
  <pkg>/tables.py        SQLAlchemy Core tables. metadata + outbox/inbox/idempotency_keys already exist.
                         Also: PLATFORM_SCOPED = {table: reason}
  <pkg>/api/*.py         FastAPI routers: /api/v1/... (public), /internal/v1/... (service-only),
                         /webhooks/<provider>/... (provider-signed, no user auth)
  <pkg>/api/__init__.py  routers = [...]
  <pkg>/main.py          create_app(settings=None, **test_overrides) -> FastAPI ; app = create_app()
  <pkg>/worker.py        celery_app + tasks (only if the service owns queues)
  <pkg>/providers/*.py   provider adapters: real + fake
  <pkg>/seed.py          async def seed(db: Database) -> None   (idempotent local demo data)
  migrations/versions/000N_*.py   0001 baseline exists; add 0002+ with scripts/gen_migration.py
  tests/conftest.py, tests/test_*.py
```

## 2. Shared library (`packages/nexus_core`): use it, do not re-implement

| Need | Use |
|---|---|
| App factory | `nexus_core.app.create_service_app(settings, title=, routers=, outbox=, inbox=, event_handlers=, allowed_producers=, periodic_jobs=, on_startup=, on_shutdown=, authz_resolver=)` |
| User authN/authZ | `nexus_core.authz`: `AuthContext`, `require_permission("x:y")`, `require_step_up()`, `AuthDep` |
| Internal endpoints | `require_internal("svc-a", "svc-b")` → `InternalContext(principal, tenant_id, actor)`; `require_service(...)` when no tenant is involved |
| Calling another service | `nexus_core.clients.service_client(request.app, "crm-service")` → `.get_json(path, tenant_id=, actor=)`, `.post_json(path, body, tenant_id=, actor=, idempotency_key=)`. Raises `UpstreamError` (`.upstream_status`, `.is_not_found`) or `UpstreamUnavailable`. Base URL comes from `settings.service_url(name)` |
| Tenant settings / kill switches | `nexus_core.clients.tenant_settings_section(client, redis, tenant_id, "communication")` (5 s cache) |
| DB sessions (RLS) | `request.app.state.db.tenant_session(tenant_id, actor)` and `.global_session()` |
| RLS in migrations | `nexus_core.rls.apply_tenant_rls`, `apply_append_only_rls`, `grant_platform_table` |
| Audit | `nexus_core.audit.emit_audit(session, outbox, producer=, actor=, tenant_id=, action=, resource_type=, resource_id=, outcome=, metadata=)` |
| Events | `nexus_core.messaging.enqueue(session, outbox, nexus_events.make_event(...))` |
| Idempotency | `nexus_core.idempotency.IdempotencyStore(idempotency_keys)`: `.begin(...)`, `.succeed(...)`, `.fail_retryable(...)` |
| Pagination | `nexus_core.pagination`: `Limit`, `Cursor`, `apply_keyset`, `build_page` |
| Rate / concurrency limits | `nexus_core.ratelimit`: `enforce_fixed_window`, `take_token`, `acquire_slot` / `release_slot` |
| Provider circuit breaker | `nexus_core.circuit.CircuitBreaker(redis, name)` |
| Celery | `nexus_core.celery_app`: `create_celery`, `TaskRuntime`, `JobContext`, `enqueue_task`, `PermanentFailure`, `TransientFailure` |
| Periodic jobs | `nexus_core.scheduling.PeriodicJob(name, interval_seconds, func)` passed to `create_service_app(periodic_jobs=[...])` (Redis-locked, ADR-0014) |
| Object storage | `nexus_core.storage`: `ObjectStore.from_settings`, `tenant_key`, `assert_tenant_key`, `presign_get`; `InMemoryObjectStore` in tests. Buckets: recordings, transcripts, attachments, raw-mail, imports, exports, knowledge |
| Provider credentials | `nexus_core.credentials.CredentialResolver(settings, vault, integration_client).resolve(tenant_id, "twilio")` |
| Vault | `nexus_core.vault.VaultClient`, `tenant_secret_path`, `platform_secret_path` |
| Adapter selection | `nexus_core.providers.select_adapter(settings, real=, fake=, name=)` |
| Outbound URL safety | `nexus_core.ssrf.validate_outbound_url` |
| Webhook verification | `nexus_core.webhooks`: `verify_twilio`, `SnsVerifier`, `sign_payload` / `verify_signature` |
| Live UI refresh | `nexus_core.realtime.publish_ui_event(redis, tenant_id, "call.updated", permission="calls:read", data={"call_id": ...})` |
| Errors | `nexus_core.errors`: `NotFound`, `Forbidden`, `Conflict`, `Unprocessable`, `TooManyRequests`, `StepUpRequired`, `ServiceUnavailable` |
| Tests | `nexus_core.testing`: `start_postgres`, `provision_and_migrate`, `TokenFactory`, `StaticAuthzResolver`, `auth` |
| Demo data ids | `nexus_core.devseed`: `DEMO_TENANTS`, `DEMO_USERS`, `demo_id(namespace, n)` |

## 3. Mandatory patterns

### Data and migrations (ADR-0007, ADR-0015)
- Every tenant-owned table has `tenant_id uuid NOT NULL` and `UNIQUE (tenant_id, id)`. Child tables use composite FKs `(tenant_id, parent_id)`.
- Use `created_at` / `updated_at` (UTC), status columns with CHECK constraints, and a `version` column where concurrent edits are possible (optimistic locking → 409).
- Platform-global tables are declared with `comment="platform_scoped: <reason>"` and listed in `PLATFORM_SCOPED`.
- Immutable history tables use `comment="append_only: <reason>"` plus `apply_append_only_rls`.
- **Generate migrations**: `uv run python scripts/gen_migration.py <pkg> "<title>"`. This produces explicit `op.create_table` calls. Then add the RLS and grants where the file says TODO: `apply_tenant_rls(op, table, APP_ROLE, privileges=...)` and `grant_platform_table(...)`. Grant the minimum (`SELECT, INSERT` for logs, and so on).
- Never create tables from live metadata inside a migration.
- `uv run pytest tests/security/test_migrations.py -k <pkg>` must pass. It checks drift, forced RLS on every tenant table, and the role attributes.
- All data access goes through `tenant_session` / `global_session`. Bind parameters only. Fetch by id inside the tenant session, and treat "no row" as 404.

### API
- Public endpoints live under `/api/v1/...`.
- Declare Pydantic request models (`extra="forbid"`) **and response models** (`response_model=`). Never return ORM rows blindly, and never return secrets.
- Lists return `{"items": [...], "next_cursor": str | None}` with `limit` (default 50, max 200) and `cursor`.
- Create / launch / send / book endpoints take an `Idempotency-Key` header:
  - same key, different payload → 422
  - still processing → 409
  - done → replay of the stored response
- Sensitive actions check the permission at execution time and write an audit event. These are: launch, export, recordings access, approve/send, credential changes, admin invites.
- High-risk actions also require `Depends(require_step_up())`.
- **FastAPI gotcha:** with `from __future__ import annotations`, a dependency that is a *local variable* cannot be used inside `Annotated[...]`. Write `x: T = Depends(local_dep)` instead.
- Money is integer micros of USD (`*_micros`). Durations are seconds. Phone numbers are E.164. Timestamps are ISO-8601 UTC.

### Security
- Authorization happens on the server for every endpoint. A UI that hides a control is not a control.
- Permission keys come from `services/tenant_service/tenant_service/catalogue.py`. If you need a new one, do not invent it. Put it in your report.
- Secrets are never stored in PostgreSQL, Redis, queue payloads, events, logs, audit metadata or API responses. Store `vault_ref` / `connection_id` only.
- Untrusted content (emails, transcripts, documents, tool output, CRM free text) reaches a model only as `ContentBlock(trust="untrusted", source=...)`. It never goes into system text, and it never decides authorization.
- Tenant-controlled URLs must pass `validate_outbound_url`. No redirects. A timeout on every call.
- Webhooks: verify the signature and timestamp, dedupe on a unique provider-event id, return 2xx fast, and do the heavy work on a queue. Resolve the tenant from trusted configuration (number / mailbox / connection), never from the payload.
- Logs and audit metadata hold IDs and enums only.

### Async work
- Long work never runs in a request handler.
- Pattern:
  1. Write the business row with status `queued` (plus audit and outbox) in one transaction.
  2. After commit, call `enqueue_task(celery_app, name, queue, JobContext(...), thing_id=str(id))`.
  3. A `PeriodicJob` sweeper re-enqueues rows stuck in `queued` or `processing`.
  4. The task claims the row atomically (`UPDATE ... WHERE status='queued' RETURNING`), so duplicate deliveries do nothing.
- Tasks take `ctx: dict` plus string IDs only.
- Keep the task body as a plain coroutine `async def do_x(runtime, ctx, id)` and call it with `runtime.run(...)`. Tests call the coroutine directly.
- Permanent failures (invalid recipient, opt-out, permission, bad credentials): record the state and return. Transient failures: raise `TransientFailure` (backoff with jitter, bounded). Anything that exhausts its retries is dead-lettered.
- External side effects pass an idempotency key to the provider. After an ambiguous timeout, **reconcile first**, never retry blindly.
- Queues (`nexus_core.celery_app.QUEUES`): calls.schedule, calls.dispatch, calls.provider_callbacks, voice.postprocess, emails.campaign_send, emails.inbound_process, emails.ai_draft, emails.send_approved, integrations.webhooks, imports.process, analytics.aggregate, notifications.send, ai.knowledge_ingest.

### Providers
- Adapters implement the Protocols in `packages/provider_contracts/nexus_providers`.
- Real adapters use the provider's HTTP API with timeouts.
- Fake adapters are deterministic and talk to the fake-providers container (`settings.fake_providers_url`) or run in-process.
- If you own a simulator, add `services/fake_providers/fake_providers/routers/<provider>.py` exporting `router = APIRouter(prefix="/<provider>")`. It is auto-discovered. Simulated webhooks are signed with `FAKE_PROVIDER_SECRET`.
- Credentials: `CredentialResolver.resolve(tenant_id, provider)`. In fake mode it returns `values["fake_secret"]`.

## 4. Ownership rules (parallel work)
- Edit **only your assigned directories**, plus your own fake-provider router files.
- **Do not edit** the following. Put what you need under "Integration requests" in your report, with the exact snippet.
  - `packages/**`
  - `scripts/**`
  - `docker-compose.yml`
  - `infra/**`
  - root `pyproject.toml`
  - `uv.lock`
  - other services
  - `apps/web/src/lib/nav.ts`
  - `docs/**` (except files you were assigned)
- You may add dependencies to your own service's `pyproject.toml`. `uv sync --all-packages` then changes `uv.lock` locally. **Do not commit `uv.lock`.**
- Never run `scripts/new_service.py --force`.
- Never start, stop or change Docker Compose or other people's containers. Tests use testcontainers only.
- Commit your work on your branch. Stage your own paths explicitly (`git add services/<pkg> ...`). Do not push.

## 5. Cross-service contracts

Internal calls send `Authorization: Bearer <service token>`, `X-Tenant-ID` and `X-Actor` (`user:<id>` | `service:<name>` | `system:<name>`). Servers must implement these exactly. Clients may rely on them and mock them with `respx` in tests. `(IK)` means the endpoint requires an `Idempotency-Key`.

### tenant-service (implemented)
| Endpoint | Response |
|---|---|
| `GET /internal/v1/tenants/{tenant_id}` | `{id, name, slug, status, timezone, platform_kill_switches:{calls_enabled, emails_enabled, ai_enabled}}` |
| `GET /internal/v1/tenants/{tenant_id}/settings/{section}` | The section. See the table below |

| Section | Fields |
|---|---|
| `communication` | `calls_enabled, emails_enabled, calling_window{start,end,days}, email_window, max_concurrent_calls, max_daily_calls, email_daily_quota, allowed_call_countries, ai_disclosure_enabled, recording_enabled, recording_consent_prompt, effective_calls_enabled, effective_emails_enabled` |
| `ai` | `allowed_models, monthly_budget_usd, draft_auto_send(false), data_classification_max, store_prompt_content(false), effective_ai_enabled` |
| `retention` | `recordings_days, transcripts_days, email_content_days, attachments_days` |
| `approval` | `require_approval_for_ai_email(true), escalate_sensitive_intents` |
| `profile` | `name, timezone, locale, business_hours` |

### crm-service
| Endpoint | Request → Response | Callers |
|---|---|---|
| `GET /internal/v1/contacts/{id}` | → `{contact_id, first_name, last_name, company, timezone, locale, status, tags, email_masked, phone_masked, custom_fields}` | tool-execution, voice, call, email-inbox, ai-agent |
| `GET /internal/v1/contacts/{id}/contactability?channel=phone\|email` | → `{contact_id, allowed, reasons:[suppressed\|no_consent\|no_address\|contact_inactive], phone_e164?, email?, timezone?, country?, first_name?, last_name?}` | call, email, campaign |
| `GET /internal/v1/contacts/lookup?phone=\|email=` | → `{contact_id \| null}` | call, email-inbox |
| `POST /internal/v1/contacts/batch` | `{contact_ids:[≤500], channel}` → `{items:[{contact_id, allowed, reasons, email?, phone_e164?, first_name?, last_name?, timezone?}]}` | email, campaign |
| `POST /internal/v1/segments/{id}/resolve` | `{cursor?, limit≤500, channel}` → `{items:[{contact_id}], next_cursor}` (only contactable contacts) | campaign, email |
| `POST /internal/v1/contacts/{id}/notes` (IK) | `{text≤2000, source:{type,id}}` → `{note_id}` | tool-execution |
| `POST /internal/v1/suppressions` (IK) | `{channel:phone\|email\|all, contact_id?, value?, reason:opt_out\|bounce\|complaint\|manual\|legal, source:{type,id}}` → `{suppression_id}` | call, voice, email, email-inbox |
| `GET /internal/v1/suppressions/check?channel=&value=` | → `{suppressed, reasons}` | email, email-inbox, call |

### campaign-service
| Endpoint | Response | Callers |
|---|---|---|
| `GET /internal/v1/campaigns/{id}/dispatch-state` | `{campaign_id, state, dispatch_allowed}` | call |

Dispatch model: campaign-service owns pacing (windows, rate, concurrency, attempts). It calls `POST call-service /internal/v1/calls` once per contact attempt, with idempotency key `campaign:{id}:contact:{contact_id}:attempt:{n}`. It consumes `call.completed` to free capacity and to schedule retries.

### call-service
| Endpoint | Request → Response | Callers |
|---|---|---|
| `POST /internal/v1/calls` (IK) | `{contact_id, agent_id, campaign_id?, campaign_contact_id?, scheduled_for?}` → 201 `{call_id, state}` | campaign |
| `POST /internal/v1/calls/{id}/cancel` | `{reason}` → `{call_id, state}` | campaign |
| `POST /internal/v1/calls/{id}/transfer` (IK) | `{reason}` → `{status: transferring\|unavailable}` | tool-execution |
| `GET /internal/v1/calls/{id}` | → `{call_id, state, direction, contact_id?, agent_id, campaign_id?, started_at?, ended_at?}` | voice, tool-execution, ai-agent |

### ai-gateway
Callers: voice-service, ai-agent-service.

`POST /internal/v1/ai/responses`

```jsonc
// request
{ "purpose": "voice_turn|email_draft|classification|summary|call_postprocess|eval",
  "agent_id": "uuid?", "model": "claude-...?", "data_classification": "internal|confidential|restricted",
  "system": "trusted instructions",
  "messages": [{ "role": "user|assistant", "content": [{ "text": "...", "source": "email|transcript|document|tool|user|agent", "trust": "trusted|untrusted" }] }],
  "response_schema": { }, "tool_ids": ["booking.find_slots"], "context": { "call_id": "?", "contact_id": "?", "thread_id": "?" },
  "max_tokens": 1024, "temperature": 0.2 }
// response
{ "request_id": "uuid", "text": "...", "json": { }, "stop_reason": "end_turn|max_tokens|tool_limit",
  "tool_calls": [{ "id": "...", "tool_id": "...", "status": "executed|approval_required|denied", "approval_id": "?" }],
  "usage": { "input_tokens": 0, "output_tokens": 0, "cost_micros": 0 }, "model": "...", "provider": "anthropic|fake" }
```

- The gateway exposes only granted tools to the model. Granted tools come from `mcp-registry GET /internal/v1/agents/{agent_id}/tools`.
- It executes proposed calls through tool-execution and feeds the results back as untrusted `tool` content, for at most 4 rounds.
- Policy denials return 403 `{code: "policy_denied", reason: model_not_allowed|classification_not_allowed|budget_exceeded|kill_switch|ai_disabled}`.
- `POST /internal/v1/ai/responses/stream` takes the same body and returns `text/event-stream`. Each line is `data: <json>`:
  - `{type:"text_delta", text}`
  - `{type:"tool_call", tool_id, status}`
  - `{type:"done", request_id, stop_reason, usage}`
  - `{type:"error", code}`
- `POST /internal/v1/ai/embeddings` takes `{texts:[≤64], purpose}` and returns `{vectors, model, dims: 1024}`.

### mcp-registry
| Endpoint | Response | Callers |
|---|---|---|
| `GET /internal/v1/agents/{agent_id}/tools` | `{tools:[{tool_id, version, description, input_schema, risk_class}]}` (granted, enabled, tenant-allowed only) | ai-gateway |
| `POST /internal/v1/tools/{tool_id}/grant-check` `{agent_id}` | `{granted, enabled, risk_class, requires_approval, version, input_schema, output_schema, target:{kind:internal\|mcp, ...}}` | tool-execution |

### tool-execution
Caller: ai-gateway only.

`POST /internal/v1/tools/execute` takes `{tool_id, arguments, agent_id, context:{call_id?, contact_id?, thread_id?}, idempotency_key}` and returns `{status: executed|approval_required|denied, result?, approval_id?, reason?}`.

- Identity, tenant and resource scope come from the verified headers and `context`. They **never** come from `arguments`.
- Class 3 requires a persisted human approval. Class 4 is always denied.

Seeded internal tools and their targets:

| Tool | Class | Model arguments | Target |
|---|---|---|---|
| `knowledge.search` | 0 | `{query}` | ai-agent `POST /internal/v1/knowledge/search` |
| `booking.find_slots` | 0 | `{from, to}` | booking `GET /internal/v1/availability` |
| `crm.get_contact` | 1 | `{}` (contact from context) | crm `GET /internal/v1/contacts/{contact_id}` |
| `crm.add_note` | 2 | `{text}` | crm `POST /internal/v1/contacts/{contact_id}/notes` |
| `booking.create_tentative` | 2 | `{start, end}` | booking `POST /internal/v1/bookings/tentative` |
| `call.transfer_to_human` | 2 | `{reason}` | call `POST /internal/v1/calls/{call_id}/transfer` |
| `booking.confirm` | 3 (approval) | `{booking_id}` | booking `POST /internal/v1/bookings/{id}/confirm` |

### ai-agent-service
| Endpoint | Request → Response | Callers |
|---|---|---|
| `GET /internal/v1/agents/{id}` | → `{agent_id, name, kind: voice\|email, status}` | campaign, call, email-inbox, mcp-registry |
| `GET /internal/v1/agents/{id}/runtime-config` | → `{agent_id, version, kind, system_prompt, model, voice_id?, tool_ids, greeting, disclosure_text, escalation_rules, max_call_seconds}` | voice, call |
| `POST /internal/v1/knowledge/search` | `{query, top_k≤8, source_ids?}` → `{chunks:[{chunk_id, source_id, source_name, text, score}]}` | tool-execution, email-inbox |
| `POST /internal/v1/drafting/runs` | `{thread_id, message_id, agent_id?, context_blocks:[{text, source, trust}]}` → `{run_id, intent, priority, sensitive: bool, summary, draft_text, citations:[{chunk_id, source_id, source_name}], quality:{confidence, flags:[..]}, model, provider, prompt_version}` | email-inbox |
| `POST /internal/v1/calls/postprocess` | `{call_id, contact_id?, agent_id, transcript:[{role: agent\|caller, text}]}` → `{summary, outcome, qualified, follow_up?}` (also emits `call.outcome.extracted`) | voice |

### booking-service
Caller: tool-execution.

| Endpoint | Request → Response |
|---|---|
| `GET /internal/v1/availability?from=&to=&resource_id?` | → `{slots:[{start, end, resource_id}]}` |
| `POST /internal/v1/bookings/tentative` (IK) | `{contact_id, start, end, resource_id?, source:{type,id}}` → `{booking_id, status: tentative, start, end}` (409 when the slot is taken) |
| `POST /internal/v1/bookings/{id}/confirm` (IK) | → `{booking_id, status: confirmed}` |
| `POST /internal/v1/bookings/{id}/cancel` (IK) | `{reason}` → `{booking_id, status: canceled}` |

### email-service
Caller: email-inbox-service.

| Endpoint | Request → Response |
|---|---|
| `POST /internal/v1/send` (IK) | `{category: reply\|transactional, from_address, to, subject, text, html?, headers:{"In-Reply-To"?, "References"?}, source:{type, id, version}}` → 202 `{message_id, state}`. Rejects unverified sender domains and suppressed recipients |
| `GET /internal/v1/messages/lookup?rfc_message_id=` | → `{message_id, campaign_id?, contact_id?}` or 404 |

### notification-service
Callers: any service.

| Endpoint | Request → Response |
|---|---|
| `POST /internal/v1/notifications/email` | `{template, to, category: transactional, variables}` → 202 |
| `POST /internal/v1/notifications/in-app` | `{user_id? \| permission?, title, body, link?, kind}` (tenant from header) → 202 |
| `GET /api/v1/notifications/stream` | SSE. Relays notifications and `ui:{tenant}` Redis events, filtered by the viewer's permissions |

### integration-service
Callers: any service.

| Endpoint | Response |
|---|---|
| `GET /internal/v1/connections/resolve?provider=<p>` | `{connection_id, provider, status, vault_ref, config}` or 404 |

Secret fields at `vault_ref`:

| Provider | Fields |
|---|---|
| twilio | `account_sid, auth_token` |
| ses | `region, access_key_id, secret_access_key` |
| anthropic, deepgram, elevenlabs, voyage | `api_key` |
| google_calendar | `client_id, client_secret, refresh_token` |
| hubspot | `access_token` |

### Events
- Types and payload models live in `packages/event_schemas/nexus_events/registry.py`. Payloads carry IDs and enums only.
- Consumers pass `allowed_producers` so that a service cannot forge another context's events.

## 6. Tests (no phase is done without them)
- Copy the `services/tenant_service/tests/conftest.py` pattern: one PostgreSQL container per session through `start_postgres` and `provision_and_migrate`.
- Create the app with `outbox_relay_enabled=False, event_consumers_enabled=False, periodic_jobs_enabled=False`.
- Inject `StaticAuthzResolver` and `tokens.verifier()`. Mock other services with `respx`.
- Mark integration tests with `pytest.mark.integration`, and add `pytest.mark.security` for isolation and authorization tests.
- Required for every service:
  - a cross-tenant negative test for each tenant-owned resource (read, update, delete, export)
  - a 403 test for each permission boundary
  - idempotency tests (replay, and the same key with a different payload)
  - webhook signature, replay and duplicate tests where applicable
  - state-machine unit tests
  - provider adapter contract tests against the fake
  - the consumer handlers, called directly with duplicate events
- Run:
  - `uv run pytest services/<pkg> -q`
  - `uv run pytest tests/security/test_migrations.py -k <pkg> -q`
  - `uv run ruff check services/<pkg> && uv run ruff format services/<pkg>`

## 7. Final report format (agents)
1. Implemented scope.
2. Files changed.
3. Tests run, with exact pass/fail counts.
4. Security considerations.
5. Known limitations.
6. **Integration requests**: compose workers and env, Traefik routes, Vault policy paths, broker publish prefixes, new permissions or events, fake routers, extra dependencies.
7. Public API summary for the UI: method, path, permission.
