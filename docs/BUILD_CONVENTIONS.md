# NexusCall — Build Conventions (binding for every contributor and coding agent)

Read with: `build.md` (requirements), `docs/ARCHITECTURE.md`, `docs/SERVICE_MAP.md`, `docs/THREAT_MODEL.md`, `docs/adr/`.
**The reference implementation is `services/tenant_service`.** Copy its patterns.

## 1. Service layout (one bounded context per service)
```text
services/<pkg>/
  <pkg>/settings.py      Settings(ServiceSettings) – env-driven, no secrets in code
  <pkg>/tables.py        SQLAlchemy Core tables (metadata + outbox/inbox/idempotency already present)
                         PLATFORM_SCOPED = {table: reason}; TENANT_TABLES = (...)
  <pkg>/api/*.py         FastAPI routers under /api/v1/... (public) and /internal/v1/... (service-only)
  <pkg>/api/__init__.py  routers list
  <pkg>/main.py          create_app(settings=None, **test_overrides) -> FastAPI ; app = create_app()
  <pkg>/worker.py        Celery app + tasks (if the service owns queues)
  <pkg>/providers/*.py   provider adapters: real + fake (fakes call FAKE_PROVIDERS_URL or are in-process)
  migrations/versions/000N_*.py   Alembic; 0001 baseline already exists – add 0002+
  tests/conftest.py      copy services/tenant_service/tests/conftest.py pattern
  tests/test_*.py
```

## 2. Mandatory patterns
- **Tenant data:** every tenant-owned table has `tenant_id uuid NOT NULL`, `UNIQUE (tenant_id, id)` and composite FKs `(tenant_id, x_id)`. The migration must call `nexus_core.rls.apply_tenant_rls(op, table, "<db>_app", privileges=...)`. Platform-global tables call `mark_platform_scoped` and `grant_platform_table`. `<db>` is the database name in `scripts/gen_compose.py SERVICES`.
- **Database access** goes only through `request.app.state.db.tenant_session(tenant_id, actor)`, or `global_session()` for platform-scoped tables. Never raw connections, never string-built SQL with values (bind parameters only).
- **AuthN/Z:** use `nexus_core.authz` dependencies.
  - Public endpoints: `Annotated[AuthContext, Depends(require_permission("x:y"))]`.
  - High-risk endpoints also take `Depends(require_step_up())`.
  - Internal endpoints: `Depends(require_service("svc-a", "svc-b"))`.
  - The permission catalogue lives in `services/tenant_service/tenant_service/catalogue.py`. Use those keys and do not invent new ones. If you need one, report it.
- **Resource ownership:** always fetch by id *inside* the tenant session. RLS returning nothing must become 404.
- **Audit:** use `nexus_core.audit.emit_audit(session, outbox, producer=..., actor=..., tenant_id=..., action=..., resource_type=..., resource_id=..., metadata=...)` in the same transaction as the change. Metadata holds IDs and enums only. Never put bodies, emails, phone numbers or tokens in it.
- **Events:** `nexus_core.messaging.enqueue(session, outbox, make_event(...))` in the same transaction.
  - Use event types that already exist in `packages/event_schemas/nexus_events/registry.py`.
  - Consumers go in `create_service_app(..., event_handlers={type: handler}, allowed_producers={type: {"producer"}})`. Handlers receive `(envelope, session)`, with the session already tenant-scoped and inbox-deduplicated.
- **Idempotency:** create/launch/send/book endpoints accept an `Idempotency-Key` header and use `nexus_core.idempotency.IdempotencyStore(idempotency_keys)` in the same transaction. Same key with a different payload gives 422. Still processing gives 409. A completed key replays the stored response.
- **Celery:** `nexus_core.celery_app.create_celery(name, settings.amqp_url.get_secret_value())`.
  - Tasks take `ctx: dict` (a `JobContext`) plus IDs only.
  - Raise `PermanentFailure` / `TransientFailure`.
  - Business state lives in the service DB, never in the result backend.
  - Tasks run async work via `asyncio.run(...)` with their own `Database`.
- **Secrets:** use `nexus_core.vault.VaultClient` with paths from `tenant_secret_path` / `platform_secret_path`. Queue payloads carry `connection_id`, never secret values. Never log or return secrets.
- **Providers:** select with `nexus_core.providers.select_adapter(settings, real=..., fake=..., name=...)`. Fakes are deterministic and only legal in development/test.
- **Outbound HTTP to tenant-controlled URLs:** `nexus_core.ssrf.validate_outbound_url` first, no redirects, timeouts on every call.
- **Webhooks:** verify with `nexus_core.webhooks` (Twilio / SNS / HMAC), dedupe on a provider event id unique constraint, and return 2xx fast. Heavy work goes to a queue.
- **Untrusted content** (emails, transcripts, documents, tool output) goes to LLMs only as labelled data blocks (`ContentBlock(trust="untrusted")`), never inside system instructions.
- **Errors:** raise `nexus_core.errors.*` (RFC 7807). Never echo PII back in errors.
- **Timestamps:** UTC in storage.

## 3. Ownership rules (to avoid conflicts between parallel contributors)
- Edit **only your assigned service directories**, plus your own router file `services/fake_providers/fake_providers/routers/<provider>.py` if assigned.
- **Do not edit** the shared files below. Put what you need in your final report as "Integration requests" (exact snippet). The integrator applies them.
  - `packages/**`
  - `scripts/**`
  - `docker-compose.yml`
  - `infra/**`
  - `pyproject.toml` at the root
  - `uv.lock`
  - other services
  - `services/fake_providers/fake_providers/routers/__init__.py`
  - `apps/web/src/lib/nav.ts`
- You may add dependencies to **your own service's** `pyproject.toml`. Mention them in the report; the integrator re-locks.
- Never run `scripts/new_service.py --force`.
- Never stop or modify running containers. Tests use testcontainers only (one Postgres per test session; stop it afterwards).

## 4. Cross-service contracts (implement servers exactly; clients may assume them)
All internal calls use a service bearer token (`app.state.service_token()`), plus `X-Tenant-ID` and `X-Actor` (`user:<id>` / `system:<name>`) headers where tenant-scoped.

| Server | Endpoint | Request → Response |
|---|---|---|
| tenant-service | `GET /internal/v1/tenants/{id}/settings/{section}` | → section JSON. `communication` adds `effective_calls_enabled`, `effective_emails_enabled` |
| crm-service | `GET /internal/v1/contacts/{id}/contactability?channel=phone\|email` | → `{contact_id, allowed: bool, reasons: [..], phone_e164?, email?, timezone?, name?}` |
| crm-service | `POST /internal/v1/segments/{id}/resolve` `{cursor?, limit<=500, channel}` | → `{items:[{contact_id}], next_cursor}` (suppression/consent filtered) |
| ai-gateway | `POST /internal/v1/ai/responses` | `{purpose, agent_id?, model?, system, messages:[{role, content:[{text, source, trust}]}], response_schema?, tools?:[tool_id], max_tokens, stream:false}` → `{request_id, text, json?, tool_calls:[{id, tool_id, arguments}], usage:{input_tokens, output_tokens, cost_micros}, model, provider}`. Denials → 403 `{code: policy_denied, reason}` |
| ai-gateway | `POST /internal/v1/ai/responses/stream` | same body → SSE `data: {type: text_delta\|tool_call\|done, ...}` |
| ai-gateway | `POST /internal/v1/ai/embeddings` `{texts:[..], purpose}` | → `{vectors:[[float]], model, dims}` |
| tool-execution | `POST /internal/v1/tools/execute` | `{tool_id, arguments, agent_id, context:{call_id?, contact_id?, thread_id?}, idempotency_key}` → `{status: executed\|approval_required\|denied, result?, approval_id?, reason?}` |
| mcp-registry | `POST /internal/v1/tools/{tool_id}/grant-check` `{agent_id}` | → `{granted, risk_class, input_schema, output_schema, requires_approval, enabled}` |
| ai-agent-service | `GET /internal/v1/agents/{id}/runtime-config` | → `{agent_id, version, system_prompt, model, voice_id?, tools:[tool_id], greeting, disclosure_text, escalation_rules}` |
| ai-agent-service | `POST /internal/v1/knowledge/search` `{query, top_k<=8, source_ids?}` | → `{chunks:[{chunk_id, source_id, text, score}]}` (tenant-filtered inside the SQL) |
| ai-agent-service | `POST /internal/v1/drafting/runs` `{thread_id, message_id, context_blocks:[..]}` | → `{intent, priority, summary, draft_text, citations:[{chunk_id, source_id}], quality:{confidence, flags}}` |
| booking-service | `POST /internal/v1/bookings/tentative` (Idempotency-Key) `{contact_id, start, end, resource_id?, source:{call_id?}}` | → booking |
| booking-service | `GET /internal/v1/availability?from&to&resource_id?` | → `{slots:[{start, end}]}` |
| call-service | `POST /internal/v1/calls` (Idempotency-Key) `{contact_id, campaign_id?, agent_id, scheduled_for?}` | → call |
| email-service | `POST /internal/v1/send` (Idempotency-Key) `{category, sender_identity_id?, to, subject, text, html?, headers:{In-Reply-To?, References?}, source:{type, id, version}}` | → `{message_id, state}` |
| notification-service | `POST /internal/v1/notifications/email` | `{template, to, category, variables}` → 202. Also `POST /internal/v1/notifications/in-app` `{tenant_id, user_ids?, permission?, title, body, link}` |
| audit-service | (consumes `audit.event` only) | |

Internal tools registered by mcp-registry (seed): `crm.get_contact` (C1), `crm.add_note` (C2), `booking.find_slots` (C0), `booking.create_tentative` (C2), `booking.confirm` (C3, approval), `call.transfer_to_human` (C2), `knowledge.search` (C0).

## 5. Tests (no phase is done without them)
- Integration tests are marked `pytest.mark.integration`, and `security` where relevant.
- Use `nexus_core.testing` (`start_postgres`, `provision_and_migrate`, `TokenFactory`, `auth`).
- Mock other services' HTTP with `respx`.
- Every new tenant-owned table needs a cross-tenant negative test, and every permission boundary needs a 403 test.
- Run them with `uv run pytest services/<pkg> -q`, `uv run ruff check services/<pkg>`, `uv run ruff format services/<pkg>`.

## 6. Final report format (agents)
1. Implemented scope.
2. Files changed.
3. Tests run, with exact pass/fail counts.
4. Security considerations.
5. Known limitations.
6. **Integration requests:** compose workers/env, Traefik routes, Vault policy paths, new permissions/events/fake routers, extra deps.
