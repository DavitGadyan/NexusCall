# ADR-0012: AI Gateway, MCP registry and tool execution as the AI security boundary

- Status: Accepted
- Date: 2026-11-25

## Decision
Three separate services:
- **ai-gateway** is the only component holding model-provider credentials. For each request it authenticates the caller's service and the actor context, then applies the following policy checks before routing to the provider adapter:
  - tenant model allowlist
  - data-classification rule
  - budget
  - kill switch
  - size limits

  It streams responses, records `ai_requests` and usage (no prompt content by default), and runs a circuit breaker per provider.
- **mcp-registry** is the catalogue of MCP servers and tools: versioned JSON Schemas, risk classes 0–4, and agent-tool grants. Policy changes go through proposals with approval; dual approval applies to high-risk changes.
- **tool-execution** is the only component allowed to run a tool. For every call it:
  1. Resolves the registered `tool_id`. Arbitrary URLs are never accepted.
  2. Checks the grant with mcp-registry.
  3. Verifies the actor and tenant from the signed context. Model-supplied identity is ignored.
  4. Checks resource ownership with the owning service.
  5. Applies risk-class policy: Class 3 requires an `ai_approval_requests` row approved by a human, and Class 4 is never agent-accessible.
  6. Injects credentials from Vault.
  7. Enforces egress through the SSRF guard.
  8. Validates the output schema.
  9. Records the execution and emits audit events.

**Email default:** AI email replies are drafts. There is no auto-send policy in the MVP.

**Prompt construction:** system policy stays separate. Untrusted content goes in as labelled data blocks `<untrusted source="email" tenant=... trust="low">`, and model output must match a JSON schema.

## Consequences
- An extra network hop for every LLM call, accepted for the security benefit. Voice latency is mitigated by streaming and by colocating the gateway.
- Agents can never hold provider or tool credentials.
