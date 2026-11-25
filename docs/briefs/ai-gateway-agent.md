# Brief: ai-gateway, ai-agent-service

**Paths you own:** `services/ai_gateway`, `services/ai_agent_service`, `services/fake_providers/fake_providers/routers/anthropic.py`, `services/fake_providers/fake_providers/routers/voyage.py`.
**Databases / app roles:** `aigw` / `aigw_app`, `agent` / `agent_app` (the `vector` extension exists in `agent`).
**build.md:** §3.1 I; all of §24; §25; §10.1.

**First step:** invoke the `claude-api` skill and follow it for the Anthropic adapter. Use the official `anthropic` Python SDK (already a dependency) with the async client, streaming, tool use and structured output.

Default models:

| Use | Model |
|---|---|
| Voice turns | `claude-haiku-5-5` |
| Drafts and summaries | `claude-sonnet-5-5` |
| Evaluations | `claude-opus-5-5` |

## ai-gateway
- Tables (§24.9):
  - `ai_providers`* and `ai_models`*: model id, capabilities, input and output price in micros per MTok, enabled, allowed data classifications.
  - `routing_policies`: purpose → ordered models, and whether failover is allowed.
  - `tenant_model_allowlists`
  - `ai_budget_policies`: monthly limit per tenant or agent, in micros.
  - `ai_requests`: metadata only. **Never store prompt or completion text.**
  - `ai_usage_records`
  - `ai_security_events`
  - `ai_kill_switches`*: scope platform | provider | model | tenant | agent.

  (* = platform-scoped.)
- Internal API exactly as in conventions §5: responses, stream, embeddings.
- Policy pipeline before every provider call, in this order:
  1. Caller allowlist.
  2. Tenant `ai` settings: `effective_ai_enabled`, `allowed_models`, `data_classification_max`.
  3. Kill switches.
  4. Model resolution. The model must be platform-enabled, tenant-allowed and allowed for the classification.
  5. Budget. On a breach, deny and emit `ai.budget.exceeded` once per period.
  6. Size limits.
  7. Per-tenant rate and concurrency.
  8. Circuit breaker per provider.

  Failover happens only if the routing policy allows it and the fallback passes every check above.
- Prompt assembly:
  - `system` stays separate.
  - Each untrusted block is wrapped as clearly delimited, source-labelled data. This is defense in depth, not the security boundary.
  - Secrets never go into prompts.
- Tools:
  1. Take the granted specs from mcp-registry and intersect them with the requested `tool_ids`.
  2. Send each proposed call to `tool-execution /internal/v1/tools/execute`. Use the context, and an idempotency key built from the request id and the tool call id.
  3. Feed the result back as untrusted tool content. Stop after 4 rounds.

  A proposal for an ungranted or unknown tool is never forwarded. Record a security event and continue.
- Output: when `response_schema` is given, validate the JSON against it. Allow one repair retry, then return the error `invalid_model_output`.
- Metering: tokens, cost, latency. Emit `ai.request.completed` with IDs and metrics only.
- Providers:
  - Anthropic, real. The key comes from `CredentialResolver.resolve(tenant, "anthropic", allow_platform_default=True)`.
  - Fake: the same adapter code pointed at `settings.fake_providers_url + "/anthropic"`, so the real code path is exercised, streaming included.
  - Embeddings: Voyage real (`voyage-3`, 1024 dimensions, httpx), plus a fake.
- Fake routers. They are deterministic and speak the Messages API wire shape, including SSE streaming.
  - They return schema-valid JSON for draft, classification and postprocess requests.
  - For voice they return short replies. When the caller mentions booking, they propose `booking.find_slots` and then `booking.create_tentative`, if those tools are offered.
  - **Adversarial hook:** when untrusted content contains `IGNORE ALL PREVIOUS INSTRUCTIONS`, the fake model obeys the injected text as far as it can. For example it proposes the tool calls named there, even ungranted ones, or it claims approval. This lets tests prove that the platform blocks the action, not the model.
  - `voyage.py` returns deterministic hash-based unit vectors.
- Public API:
  - `GET /api/v1/ai/models`
  - `GET /api/v1/ai/usage` (`ai:usage:read`)
  - `GET` / `PUT /api/v1/ai/budgets` (`tenant:manage`, bounded by the platform maximum)
- Platform API (`platform:ai:read`; changes need `platform:ai:manage` and step-up, and are audited): providers, models, routing, tenant allowlists, `POST /api/v1/platform/ai/kill-switches`, security events, usage.
- Tests (§24.11):
  - An unapproved model is denied.
  - The allowlist and classification are enforced before any provider call. Assert that the provider mock was not called.
  - Budget, kill switch and breaker work.
  - An ungranted tool proposal is not executed.
  - An injection in untrusted content cannot trigger tools outside the grants.
  - Schema validation works.
  - Cross-tenant isolation holds.
  - No prompt text is persisted or logged.

## ai-agent-service
- Tables:
  - `agents`: kind voice|email, status, current version.
  - `prompt_versions`: **append-only**. System prompt, greeting, disclosure text, model, voice id, temperature, tool ids, escalation rules, max call seconds.
  - `knowledge_sources`: with a classification.
  - `knowledge_documents`
  - `knowledge_chunks`: `Vector(1024)`, text, with RLS.
  - `eval_datasets`, `eval_cases`, `eval_runs`
  - `drafting_runs`: provenance metadata only.
- Public API (`ai:agents:read` / `ai:agents:manage`):
  - Agents CRUD, versions, activate (audited).
  - Knowledge sources.
  - Document upload: txt, md or pdf, up to 10 MB, into the `knowledge` bucket. The `ai.knowledge_ingest` task then extracts with limits, chunks, embeds through the gateway and inserts.
  - A search test endpoint.
  - Evaluations: datasets, cases, and a run that includes injection cases.
- Internal API exactly as in conventions §5.
  - `knowledge/search` puts the tenant filter in the SQL itself, backed by RLS, and respects the classification cap.
  - `drafting/runs` goes classify → summarize → retrieve → draft, all through the gateway with `response_schema`. Email text is passed as untrusted blocks. The drafting call requests **no tools**. It returns provenance.
  - `calls/postprocess` returns a structured outcome and emits `call.outcome.extracted`.
- No internal endpoint may change an agent's configuration.
- Seed, per demo tenant:
  - A voice agent `demo_id("agent", 1)` for acme and `demo_id("agent", 101)` for globex.
  - An email agent, 2 and 102.
  - Both active, with a small knowledge source: hours, services, pricing FAQ.
- Tests:
  - Retrieval isolation, including a raw SQL probe.
  - The classification cap.
  - Drafting provenance, and an injection in an email cannot add tools.
  - Ingestion limits.
  - Permission boundaries.
