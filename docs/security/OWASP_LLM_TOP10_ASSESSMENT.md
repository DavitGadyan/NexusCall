# OWASP Top 10 for LLM Applications 2026 — gap assessment

- **Source requirements:** [`owasp_sec.md`](../../owasp_sec.md). Platform design: [`build.md`](../../build.md) §24, [ADR-0012](../adr/0012-ai-gateway-boundary.md).
- **Scope:** the **AI Gateway** (`services/ai_gateway`), which is built and tested, plus the controls that belong to components not yet built. Those are marked *assigned* and carried into the relevant build briefs.
- **Date:** 2026-11-26.

> This is not a statement that the application is secure or "OWASP compliant". It records the controls that exist, the tests that verify them, and what remains open. `owasp_sec.md` §17 sets the bar: a control is complete only when the backend enforces it at the right trust boundary, positive and negative tests pass, it is auditable, and it is documented.

Status legend:
- **Implemented:** enforced in code and covered by passing tests.
- **Partial:** some of the control exists, or it is verified only in part.
- **Assigned:** designed and briefed, but its component is not built yet.
- **Missing:** not yet designed or built.

## 1. Trust boundaries in the AI path

```text
caller service ──(service JWT + azp allowlist, X-Tenant-ID, X-Actor)──▶ AI Gateway
   │                                                                    │ policy: tenant AI switch, kill switches,
   │                                                                    │ classification, model allowlist, budgets,
   │                                                                    │ rate/concurrency, size limits
   │                                                                    │ content: normalize → detect → redact → spotlight
   │                                                                    ├──▶ model provider (endpoint from config only)
   │                                                                    ├──▶ mcp-registry   (what this agent is granted)
   │                                                                    └──▶ tool-execution (per-call authz, approvals,
   │                                                                          Vault credentials, resource ownership)
   ◀── validated, sanitized output + real tool statuses + request_id ───┘
```

Data that crosses from external parties into model context is **always untrusted**. That covers emails, transcripts, documents, CRM free text, knowledge chunks and tool output. The caller cannot mark it trusted: `schemas.ContentIn.effective_trust` overrides any such claim.

## 2. Assessment

Evidence paths are relative to `services/ai_gateway/`. Tests are in `tests/test_owasp_llm_top10.py` unless named otherwise.

### LLM01 — Prompt injection — **Implemented (gateway)**, Assigned (call/email agents)
| Control | Evidence |
|---|---|
| Tool authorization outside the model. A proposed call runs only if the registry grants that tool to this agent, and tool-execution authorizes it again | `orchestrator._granted_tools`, `_run_tools`, `_execute_tool`; tests `test_llm01_indirect_injection_cannot_call_a_tool_that_was_not_granted`, `..._not_offered_is_never_exposed` |
| High-impact actions need a persisted human approval, which model text cannot claim | `_execute_tool` reports `approval_required`; test `test_llm01_granted_high_impact_tool_still_needs_backend_approval` (fake "already approved" claim) |
| Identity, tenant and resource scope come from verified headers and context, never from tool arguments | `_execute_tool` body/headers; same test asserts the headers |
| Spotlighting: per-request random delimiter, forged delimiters neutralized, untrusted-data rules in the hidden context | `guards.wrap_untrusted`, `untrusted_handling_rules`; test `test_llm01_external_content_cannot_be_upgraded_to_trusted_and_is_spotlighted` |
| Unicode normalization: NFKC; zero-width, bidi, tag-character and ANSI stripping | `guards.normalize_text`; test `test_llm01_hidden_unicode_and_encoded_payloads_are_normalized_and_flagged` |
| Injection signals as detection only: plain, base64/hex-decoded, hidden-tag text, multilingual, delimiter spoofing, fake authorization. They never decide authorization | `guards.detect_injection_signals`; tests `..._multilingual_and_delimiter_injection_signals` (5 cases), `test_injection_signals` |
| Tool output treated as untrusted, scanned and spotlighted | `_execute_tool`; test `test_llm01_tool_output_is_untrusted` |
| *Assigned:* persistent memory writes (none exist yet); voice and email agents restricted to narrow tool sets | briefs `ai-gateway-agent.md`, `calls-voice.md`, `email-inbox.md` |

### LLM02 — Sensitive information disclosure — **Implemented (gateway)**, Partial (platform)
| Control | Evidence |
|---|---|
| Secrets redacted from every input block, tool output and model output: provider keys, AWS, GitHub, Slack, Vault, JWT, private keys, connection strings, password assignments | `guards.redact_secrets`; tests `test_llm02_secrets_are_redacted_before_reaching_the_model`, `test_llm02_secrets_produced_by_the_model_are_redacted_from_output`, `test_secrets_found_and_redacted` |
| Data minimization: PII redacted for `restricted` data; `restricted` data refused unless the tenant allows it | `orchestrator._assemble`, `policy.check_classification`; tests `test_llm02_pii_is_minimized_for_restricted_data_only`, `..._classification_above_tenant_policy_never_reaches_a_provider`, `test_pii_redaction` |
| No prompt or completion text persisted: requests, security events and outbox hold metadata, hashes and counts only | `tables.py` (no content columns), `persist`; test `test_llm02_no_prompt_or_completion_text_is_stored_or_logged` |
| No prompt text in logs. **Finding fixed:** the Anthropic SDK logged full request bodies at DEBUG. SDK and HTTP-client loggers are now pinned to WARNING platform-wide | `nexus_core/logging.QUIET_LOGGERS`; same test (caplog at DEBUG) |
| Tenant isolation of AI records: forced RLS, append-only requests and events; the cross-tenant platform view goes only through a read-only role and is audited | migration `0002`, `api/admin.platform_security_events`; tests `test_llm02_tenant_cannot_see_another_tenants_ai_records`, `tests/security/test_migrations.py::...[ai_gateway]` |
| No response cache, so no cross-tenant cache reuse | by design (not built) |
| *Partial:* encryption at rest for databases, backups and logs is a deployment concern (CloudNativePG, Vault); outbound DNS/egress restriction depends on NetworkPolicies | `infra-hardening.md` brief |

### LLM03 — Excessive agency — **Implemented (gateway)**, Assigned (tool-execution, mcp-registry)
| Control | Evidence |
|---|---|
| Exposed tools = requested ∩ registry-granted. Class 4 (privileged) tools are never exposed | `_granted_tools`; test `test_llm03_privileged_tools_are_never_exposed` |
| Tools only for registered agents | test `test_llm03_tools_require_a_registered_agent` |
| Strict schema validation of arguments before forwarding; extra or "override" fields rejected | `_run_tools` (jsonschema); test `test_llm03_tool_arguments_are_schema_validated_before_execution` |
| Fail closed when the registry or policy source is down | test `test_llm03_fails_closed_when_the_tool_registry_is_unavailable` |
| No open-ended tools: no shell, SQL, URL fetch or code execution | tool catalogue (BUILD_CONVENTIONS §5); no such tool exists |
| Separate identity for each calling service; caller allowlist | `settings.internal_client_allowlist`; test `test_only_allowlisted_services_with_a_tenant_can_call_the_gateway` |
| *Assigned:* per-call resource ownership, Class 3 approvals, approval consumed once, least-privilege downstream credentials | `mcp-tools.md` brief |

### LLM04 — Supply chain — **Partial**
| Control | Evidence |
|---|---|
| Only registered and enabled models; routing and allowlists reference registered models only; platform can disable a model with step-up and audit | `policy.resolve_models`, `api/admin.patch_model`, `put_route`; tests `test_llm04_only_registered_models_can_be_used`, `..._disabled_or_tenant_excluded_models_are_skipped` |
| Provider endpoints come from configuration, never from the request | `settings.anthropic_base_url`; test `test_llm04_provider_endpoint_is_not_request_controlled` |
| Pinned dependencies with hashes (`uv.lock`, `pnpm-lock.yaml`); pnpm install scripts restricted to an allowlist; Trivy image scan and gitleaks in CI | `uv.lock`, `pnpm-workspace.yaml`, `.github/workflows/ci.yml` |
| SBOM (CycloneDX) generation per image in CI | `.github/workflows/ci.yml` (Syft) — added with this assessment |
| *Missing:* signed images and attestations (Sigstore), ML-BOM. No self-hosted models exist, so model-artifact signing does not apply yet | residual risk R-AI-4 |
| *Assigned:* MCP server allowlist, version pinning, endpoint validation | `mcp-tools.md` brief |

### LLM05 — Data and model poisoning — **Partial (gateway)**, Assigned (ai-agent-service)
| Control | Evidence |
|---|---|
| Knowledge and RAG content keeps its provenance and is always spotlighted as untrusted | test `test_llm05_operator_and_knowledge_sources_keep_their_provenance` |
| No fine-tuning or training pipelines exist (N/A today) | — |
| *Assigned:* ingestion pipeline (file validation, malware scan, provenance, PII classification, approval for high-risk sources, `approved` set only by the trusted workflow), versioned sources, poisoning and adversarial evaluations | `ai-gateway-agent.md` brief §OWASP |

### LLM06 — Unbounded consumption — **Implemented**
| Control | Evidence |
|---|---|
| Size limits: messages, blocks, characters, system text | `prepare` step 2; test `test_llm06_oversized_input_is_rejected` |
| Output-token ceilings per purpose | `settings.PURPOSE_MAX_TOKENS`; test `test_llm06_output_tokens_are_capped_per_purpose` |
| Monthly budgets (tenant and agent) as hard caps, checked before spending with a worst-case estimate; per-request cost cap; budget-exceeded event once per period | `policy.budget_state/check_budget`; test `test_llm06_monthly_budget_is_a_hard_cap` |
| Per-tenant and per-agent request rates; per-tenant concurrency slots that expire | `prepare` step 5, `Gateway.slot`; test `test_llm06_request_rate_and_concurrency_limits` |
| Agent loop limits: tool rounds, calls per round, total calls, repeated-call detection, wall-clock deadline per purpose | `_run_tools`, `PURPOSE_DEADLINE_SECONDS`; test `test_llm06_agent_loops_are_bounded` |
| Kill switches: platform, provider, model, tenant, agent; tenant AI switch | `policy.kill_switch_reason`; tests `test_llm06_kill_switches_stop_requests_immediately`, `test_platform_controls_need_platform_permission_and_step_up` |
| Circuit breaker per provider, shared across replicas (Redis) | `Gateway.breaker`; test `test_llm06_circuit_breaker_stops_calling_a_failing_provider` |

### LLM07 — Misinformation — **Partial**
| Control | Evidence |
|---|---|
| Real tool statuses are returned to callers (`executed`, `approval_required`, `denied`, `failed`...), never model claims. The model is told an action was not performed | `ToolCallOut`, `_execute_tool`; tests `test_llm07_failed_tools_are_reported_as_failed`, `test_llm07_booking_flow_reports_real_statuses` |
| Structured outputs validated against strict schemas; one repair attempt; then fail closed | `_validated_json`; tests `test_llm10_structured_output_is_schema_validated`, `test_llm10_invalid_structured_output_fails_closed` |
| *Assigned:* Claim-Check-Act. Booking and email flows verify claims against authoritative services; AI email replies are always reviewed by a human (no auto-send) | `email-inbox.md`, `booking-integration.md` briefs |

### LLM08 — Hidden context exposure — **Implemented**
| Control | Evidence |
|---|---|
| Credentials can never enter the hidden context: a system prompt with credential-like text is rejected | `prepare` step 3; test `test_llm08_secrets_in_the_system_prompt_are_rejected` |
| Canary token at both ends of the hidden context (plain, spaced-out and base64 forms detected), plus 8-word verbatim-overlap detection (whole prompt for short prompts); leaks replaced with a fixed refusal and recorded. **Finding fixed:** a 12-word window missed an 11-word verbatim leak | `guards.leaks_hidden_context`, `_check_output`; test `test_llm08_system_prompt_extraction_is_blocked` |
| Streaming holdback: text is released sentence by sentence only after the leak check, so a leak stops the stream before release. **Finding fixed:** a short system prompt could be echoed before the canary appeared; the canary now opens the hidden context | `Gateway.stream`; test `test_llm08_streaming_stops_before_hidden_context_is_released` |
| Prompt secrecy is not an authorization boundary: tool exposure and authorization never depend on hidden text | §LLM03 |

### LLM09 — Vector and embedding weaknesses — **Partial (gateway)**, Assigned (ai-agent-service)
| Control | Evidence |
|---|---|
| Embeddings endpoint: caller allowlist, tenant policy, kill switches, budget, rate limit; secrets never embedded; `restricted` data refused by the embedding model's classification | `Gateway.embed`; test `test_embeddings_redact_secrets_and_respect_classification` |
| *Assigned:* tenant filter inside the vector SQL (pgvector with forced RLS), no raw scores to ordinary clients, retrieval rate limits, embedding deletion with source deletion, re-embedding on model change, jamming tests | `ai-gateway-agent.md` brief §OWASP |

### LLM10 — Improper output handling — **Implemented (gateway)**, Assigned (renderers)
| Control | Evidence |
|---|---|
| Output sanitization: markdown images (zero-click exfiltration), active HTML, inline event handlers, `javascript:`/`data:`/`file:` schemes, ANSI and terminal control characters, invisible characters, secrets | `guards.sanitize_output`; tests `test_llm10_active_content_and_exfiltration_links_are_neutralized`, `test_output_sanitization` |
| Schema validation of structured output; tool arguments validated (not just JSON syntax) | §LLM07, §LLM03 |
| Model output never executed: no `exec`, SQL or shell from model output anywhere in the platform | code review, tool catalogue |
| *Assigned:* the web UI renders AI text as text (sanitized, sandboxed frames for email HTML, strict CSP) | `web-foundation.md`, `web-modules-calls-email.md` |

## 3. Test results (2026-11-26)

| Suite | Result |
|---|---|
| `services/ai_gateway`: OWASP suite (45) + guard unit tests (24) + smoke (2), real PostgreSQL + Redis, real Anthropic SDK against the local simulator | **71 passed** |
| `tests/security/test_migrations.py -k ai_gateway` (drift, forced RLS, append-only, runtime role) | **passed** |

Run with: `NEXUS_REQUIRE_DOCKER=1 uv run pytest services/ai_gateway tests/security/test_migrations.py -k "ai_gateway or owasp" -q`

The simulator includes an *obedient-model* mode: injected instructions in untrusted content are followed, so every attack test asserts what the platform enforced, not what a model chose to do.

## 4. Residual-risk register

| ID | Risk | Status / mitigation | Owner |
|---|---|---|---|
| R-AI-1 | Injection signals are heuristic; novel phrasings will not be flagged | By design, detection is not the boundary. Authorization, grants and approvals hold regardless. Corpus to be extended in Phase 8 adversarial testing | Phase 8 |
| R-AI-2 | Untested against a real model. A real model may behave differently from the simulator (refusals, partial tool JSON) | Contract-test the adapter against real Claude in a sandbox account when credentials are available | ops |
| R-AI-3 | Output sanitization is regex-based; a downstream renderer that injects HTML could still be exposed | Renderers must treat AI text as text (web briefs); CSP | web |
| R-AI-4 | No image signing or attestations yet; no ML-BOM | Add Sigstore cosign + SBOM attestations in the deploy pipeline (Phase 8) | Phase 8 |
| R-AI-5 | Streaming sanitization works per sentence, so an HTML element split across sentences is not reassembled before sanitizing | The leak check runs on the full text before each release; renderers display text only | web |
| R-AI-6 | Tenant AI policy is cached for up to 5 s, so a tenant AI switch takes up to 5 s to apply (platform kill switches apply immediately) | Accepted; documented | — |
| R-AI-7 | PII redaction applies to `restricted` data only; `confidential` data reaches the provider unredacted | Tenant-configurable classification caps; provider DPA review required (build.md §24.7) | legal/ops |
| R-AI-8 | RAG ingestion, vector isolation, MCP servers and tool-execution are not built yet | Assigned to briefs with explicit OWASP requirements; the gateway already fails closed without them | build |
