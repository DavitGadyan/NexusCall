# Brief: ai-agent-service (agents, prompts, knowledge/RAG, drafting)

> **Update 2026-11-26:** the **AI Gateway is built** (`services/ai_gateway`, see
> `docs/security/OWASP_LLM_TOP10_ASSESSMENT.md`). Do **not** edit it. Call it exactly per
> `docs/BUILD_CONVENTIONS.md` §5 (`/internal/v1/ai/responses`, `/embeddings`), passing knowledge chunks,
> emails and transcripts as content blocks with `source` = `knowledge` / `email` / `transcript` (they are
> always untrusted). Read `owasp_sec.md` (LLM05 and LLM09 especially).

**Paths you own:** `services/ai_agent_service`, `services/fake_providers/fake_providers/routers/voyage.py` (optional;
the gateway already has an in-process fake embedder).
**Database / app role:** `agent` / `agent_app` (the `vector` extension exists).
**build.md:** §3.1 I; §24.6, §24.7.

## OWASP requirements for this service (from owasp_sec.md; each needs a test)
- **LLM09 retrieval isolation:**
  - The tenant filter is enforced inside the similarity SQL by forced RLS on `knowledge_chunks`. Never filter after a global search.
  - Add a source ACL (`classification`, optional `allowed_roles`), derived server-side.
  - Write a raw-SQL probe test that proves tenant B's chunks are never candidates.
- **LLM09 oracle protection:**
  - Rate-limit the search endpoints.
  - Do not return raw similarity scores to ordinary (non-admin) clients; return rank only.
  - Monitor repeated retrieval failures (jamming).
- **LLM09 lifecycle:**
  - Deleting a document or source deletes its chunks and embeddings in the same transaction, and removes the stored file.
  - Changing the embedding model re-embeds the whole corpus. Track `embedding_model` on every chunk, and never mix models in one search.
- **LLM05 ingestion pipeline:** file type and size validation → (optional) malware scan → provenance (`uploaded_by`, source, version, sha256) → PII classification flag → content analysis.
  - Gateway injection signals mark a document `needs_review`.
  - Sources marked high-risk need human approval before their chunks become retrievable.
  - The `approved` flag is set only by that workflow, never by the client.
- **LLM05/LLM07 grounding:** drafting returns `citations` (chunk and source ids) for factual claims. A draft with no supporting chunk for a factual answer is flagged `ungrounded` in `quality.flags`.
- **LLM01:** the drafting call requests **no tools**.
- **Evaluations:** datasets, cases and runs that include injection and poisoning cases.

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
