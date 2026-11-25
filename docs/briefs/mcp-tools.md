# Brief: mcp-registry, tool-execution

**Paths you own:** `services/mcp_registry`, `services/tool_execution`.
**Databases / app roles:** `mcpreg` / `mcpreg_app`, `toolexec` / `toolexec_app`.
**build.md:** §24.3–24.6, §24.8–24.11, §25.

## mcp-registry
- Tables:
  - `mcp_servers`*: owner, purpose, endpoint, transport, trust level, `vault_ref`, allowed hosts, limits, health, enabled, revoked.
  - `mcp_server_versions`*
  - `tool_definitions`*: tool id, version, kind internal|mcp, target, input and output JSON Schema, risk class 0–4, side-effecting flag, requires-approval flag, limits, enabled, revoked.
  - `tenant_tool_settings`: tenants can only tighten.
  - `agent_tool_grants`
  - `ai_policy_versions`*
  - `policy_proposals`*: the approver must differ from the proposer.

  (* = platform-scoped.)
- Seed the seven internal tools from conventions §5 in the migration. Use strict schemas with `additionalProperties: false`.
- Tenant API:
  - `GET /api/v1/ai/tools` (`ai:agents:read`)
  - `GET` / `PUT /api/v1/ai/grants?agent_id=` (`ai:agents:manage`, audited). Validate the agent through ai-agent. Class 4 can never be granted. Class 3 always requires approval.
  - `GET` / `PUT /api/v1/ai/tools/settings` (`tenant:manage`)
- Platform API (`platform:ai:read`; changes need `platform:ai:manage` and step-up, and are audited):
  - MCP servers CRUD. Validate the endpoint with `validate_outbound_url` and the allowed hosts. Credentials are a Vault reference only.
  - Tool enable, disable and emergency revoke.
  - Policy proposals: propose → approve (a different admin) → deploy → rollback, with history.
- Internal API exactly as in conventions §5.
- There are no internal mutation endpoints. A model or a service can never register servers, or change tools or grants.

### OWASP additions (owasp_sec.md LLM03/LLM04)
- MCP servers are pinned: store `version` and `sha256` of the reviewed release (or the image digest), plus `reviewed_by`/`reviewed_at`. A server whose reported version or identity differs from the pinned one is disabled automatically, with a security event.
- Every MCP server has its own service identity and least-privilege credential (a Vault reference). Never share an admin credential.
- Per-agent and per-tool invocation budgets (calls per minute) and a circuit breaker per MCP server.

## tool-execution
- Tables:
  - `tool_execution_records`
  - `ai_approval_requests`: status pending|approved|rejected|expired|consumed, canonical args and their hash, context, expiry.
- `POST /internal/v1/tools/execute`. The only allowed caller is `svc-ai-gateway`. Do these in order:
  1. **Grant-check.** An unknown, disabled, revoked or ungranted tool is denied, with a security event.
  2. **Validate the arguments** against the input schema. Reject unknown fields. No schema contains identity or authority fields.
  3. **Bind resources from the verified `context`.** The contact, call and thread ids never come from the arguments. For other resources, the owning service verifies ownership.
  4. **Apply rate and concurrency limits.**
  5. **Apply the risk policy.**
     - Class 0–2: execute. Class 1 is audited as a sensitive read. Class 2 forwards an idempotency key.
     - Class 3: an **approved, unconsumed, unexpired** approval for the same tool, args hash and context must exist in the database. Model text never counts. If there is none, create a pending request, notify the people who hold `ai:approvals:decide`, and return `approval_required`.
     - Class 4: always denied, with a security event.
  6. **Execute.**
     - Internal tools: `service_client` to the owning service, with tenant, actor and idempotency headers.
     - MCP tools: a minimal streamable-HTTP JSON-RPC client (`tools/call`). It needs:
       - a host allowlist, and `validate_outbound_url` on every call
       - no redirects, timeouts, and a response size cap
       - the Vault credential injected only after authorization
  7. **Validate the output** against the output schema and limit its size.
  8. **Record and audit**, without secrets.
- Public API (`ai:approvals:decide`):
  - `GET /api/v1/ai/tool-approvals`
  - `GET /{id}`
  - `POST /{id}/approve` (step-up):
    - Check the permission at that moment.
    - The approver must be a human.
    - Execute exactly once from the stored args, then mark the approval consumed.
    - Audit it.
  - `POST /{id}/reject`
- A job expires pending approvals.
- Tests (§24.11):
  - An unregistered tool or an arbitrary URL cannot run.
  - An ungranted tool is denied.
  - Extra fields and identity fields are rejected.
  - Context binding holds.
  - Class 3 needs a database approval. A forged "approved" claim is rejected.
  - A service identity cannot approve.
  - An approval is consumed once, and a stale approval is denied.
  - Class 4 is denied.
  - An SSRF corpus for MCP endpoints: private IPs, the metadata address, redirects.
  - Credentials never appear in records, logs or responses.
  - Cross-tenant isolation.
  - Tool output that contains instructions cannot escalate privileges.
