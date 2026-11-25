"""AI Gateway request orchestration (build.md §24.1/§24.2, owasp_sec.md).

Every request runs the same fixed pipeline. Each step is deterministic code outside the model:

  1. caller + tenant + actor come from the verified service token and headers (never the body)   LLM01/LLM03
  2. size limits on messages, blocks and characters                                              LLM06
  3. no secrets in the system prompt; secrets redacted from all content                          LLM02/LLM08
  4. tenant AI switch, kill switches, data classification, model allowlist and routing           LLM02/LLM04
  5. per-tenant and per-agent rate limits, concurrency slots                                     LLM06
  6. monthly budgets and a per-request cost cap, checked with a worst-case estimate              LLM06
  7. tool exposure = requested ∩ granted by mcp-registry; every call re-authorized by
     tool-execution; arguments validated here too; per-round/total/repeat limits                 LLM01/LLM03
  8. untrusted content normalized, scanned for injection signals, PII-minimized, spotlighted      LLM01/LLM02
  9. tool output treated as untrusted; real tool statuses reported (never model claims)          LLM01/LLM07
 10. output: hidden-context leak check (canary + verbatim system text), sanitization, schema
     validation with one repair attempt                                                          LLM08/LLM10
 11. metadata-only audit: request record, usage, security events, events via outbox             LLM02
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert

from ai_gateway import guards
from ai_gateway.catalog import cost_micros
from ai_gateway.policy import (
    ModelInfo,
    PolicyDenied,
    budget_state,
    check_budget,
    check_classification,
    kill_switch_reason,
    record_usage,
    resolve_models,
    worst_case_cost,
)
from ai_gateway.providers import (
    ProviderError,
    ProviderPermanentError,
    ProviderRequest,
    ProviderResponse,
    ProviderTransientError,
)
from ai_gateway.schemas import EmbeddingsRequest, ResponseRequest, ToolCallOut
from ai_gateway.settings import PURPOSE_DEADLINE_SECONDS, PURPOSE_MAX_TOKENS, Settings
from ai_gateway.tables import ai_requests, ai_security_events, outbox
from nexus_core.authz import InternalContext
from nexus_core.circuit import CircuitBreaker
from nexus_core.clients import UpstreamError, UpstreamUnavailable, service_client, tenant_settings_section
from nexus_core.errors import ServiceUnavailable
from nexus_core.messaging import enqueue
from nexus_core.ratelimit import acquire_slot, allow_fixed_window, release_slot
from nexus_events import ActorRef, make_event

log = logging.getLogger(__name__)
PRODUCER = "ai-gateway"
BLOCKED_OUTPUT_TEXT = "I can't help with that request."
_TOOL_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


@dataclass
class SecurityEvent:
    kind: str
    severity: str
    owasp: str
    detail: str = ""
    tool_id: str | None = None


@dataclass
class RunState:
    request_id: uuid.UUID
    started: float = field(default_factory=time.monotonic)
    input_tokens: int = 0
    output_tokens: int = 0
    cost: int = 0
    rounds: int = 0
    flags: list[str] = field(default_factory=list)
    events: list[SecurityEvent] = field(default_factory=list)
    tool_calls: list[ToolCallOut] = field(default_factory=list)
    model: ModelInfo | None = None

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)

    def event(self, kind: str, severity: str, owasp: str, detail: str = "", tool_id: str | None = None) -> None:
        self.events.append(SecurityEvent(kind, severity, owasp, detail[:500], tool_id))


@dataclass
class ToolSpec:
    tool_id: str
    provider_name: str
    description: str
    input_schema: dict[str, Any]
    risk_class: int


@dataclass
class Prepared:
    req: ResponseRequest
    ctx: InternalContext
    tenant_id: uuid.UUID
    candidates: list[ModelInfo]
    failover: bool
    system_text: str
    canary: str
    messages: list[dict[str, Any]]
    tools: dict[str, ToolSpec]  # provider tool name -> spec
    max_tokens: int
    deadline: float
    request_cap: int
    input_chars: int
    untrusted_blocks: int
    fingerprint: str


def provider_tool_name(tool_id: str) -> str:
    return _TOOL_NAME_SAFE.sub("_", tool_id.replace(".", "__"))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", stripped)
    if fence:
        stripped = fence.group(1)
    return json.loads(stripped)


class Gateway:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    # ------------------------------------------------------------------ shared state
    @property
    def settings(self) -> Settings:
        return self.app.state.settings  # type: ignore[no-any-return]

    @property
    def redis(self) -> Redis | None:
        return getattr(self.app.state, "redis", None)

    def breaker(self, provider: str) -> CircuitBreaker:
        return CircuitBreaker(self.redis, f"aigw:{provider}", failure_threshold=5, window_seconds=60, open_seconds=30)

    # ------------------------------------------------------------------ preparation (steps 1-8)
    async def prepare(self, req: ResponseRequest, ctx: InternalContext, state: RunState) -> Prepared:
        s = self.settings
        tenant_id = ctx.require_tenant()

        # 2. size limits
        blocks = [b for m in req.messages for b in m.content]
        input_chars = len(req.system) + sum(len(b.text) for b in blocks)
        if len(blocks) > s.max_blocks or input_chars > s.max_input_chars or len(req.system) > s.max_system_chars:
            raise PolicyDenied("input_too_large", "The request exceeds the gateway's size limits", status=413)

        # 3. secrets never belong in the hidden context (LLM08)
        if guards.find_secrets(req.system):
            state.event("secret_in_system_prompt", "high", "LLM08", "system prompt contained credential-like text")
            raise PolicyDenied("secret_in_system_prompt", "The system prompt contains credential-like text")

        # 4. tenant switch, classification, kill switches, models
        tenant_client = service_client(self.app, "tenant-service")
        try:
            ai_settings = await tenant_settings_section(tenant_client, self.redis, tenant_id, "ai")
        except (UpstreamError, UpstreamUnavailable) as exc:
            raise ServiceUnavailable("Tenant AI policy is unavailable; failing closed") from exc
        if not ai_settings.get("effective_ai_enabled", False):
            raise PolicyDenied("ai_disabled", "AI is disabled for this tenant or platform")
        check_classification(req.data_classification, str(ai_settings.get("data_classification_max", "internal")))

        db = self.app.state.db
        async with db.tenant_session(tenant_id, ctx.actor) as session:
            reason = await kill_switch_reason(session, tenant_id=tenant_id, agent_id=req.agent_id)
            if reason:
                raise PolicyDenied(reason, "An AI kill switch is engaged")
            candidates, failover = await resolve_models(
                session,
                tenant_id=tenant_id,
                purpose=req.purpose,
                requested=req.model,
                classification=req.data_classification,
                tenant_allowed=list(ai_settings.get("allowed_models", [])),
                agent_id=req.agent_id,
            )
            budget = await budget_state(
                session,
                agent_id=req.agent_id,
                tenant_budget_usd=int(ai_settings.get("monthly_budget_usd", 0)),
                default_request_cap=s.default_request_cost_cap_micros,
            )

        # 5. rate limits (concurrency is acquired by the caller of prepare)
        if not await allow_fixed_window(
            self.redis, f"aigw:rpm:{tenant_id}:{int(time.time() // 60)}", s.tenant_requests_per_minute, 70
        ):
            raise PolicyDenied("rate_limited", "Tenant AI request rate exceeded", status=429)
        if req.agent_id and not await allow_fixed_window(
            self.redis, f"aigw:rpm:agent:{req.agent_id}:{int(time.time() // 60)}", s.agent_requests_per_minute, 70
        ):
            raise PolicyDenied("rate_limited", "Agent AI request rate exceeded", status=429)

        # 6. budgets: worst-case estimate before spending anything
        max_tokens = min(req.max_tokens, PURPOSE_MAX_TOKENS[req.purpose], candidates[0].max_output_tokens)
        rounds = (
            1 + (s.max_tool_rounds if req.tool_ids else 0) + (s.schema_repair_attempts if req.response_schema else 0)
        )
        estimate = worst_case_cost(candidates[0], input_chars, max_tokens, min(rounds, 2))
        try:
            check_budget(budget, estimate)
        except PolicyDenied:
            await self._budget_exceeded_event(tenant_id, budget)
            raise
        request_cap = int(budget["request_cap"])
        if estimate > request_cap * 2:
            raise PolicyDenied("request_cost_cap", "The request could exceed the per-request cost cap")

        # 7. tools: only what the registry grants to this agent
        tools = await self._granted_tools(req, ctx, state)

        # 8. assemble the prompt with spotlighted untrusted content
        nonce, canary = guards.new_nonce(), guards.new_canary()
        # The canary opens and closes the hidden context, so any verbatim echo trips the leak check early.
        system_text = "\n\n".join(
            [
                guards.canary_instruction(canary),
                req.system,
                guards.untrusted_handling_rules(nonce),
                guards.canary_instruction(canary),
            ]
        )
        if tools:
            system_text += (
                "\n\nTool results are reported by the platform. Never claim an action was completed "
                "unless its tool result says it was executed."
            )
        messages, untrusted_blocks = self._assemble(req, nonce, state)

        return Prepared(
            req=req,
            ctx=ctx,
            tenant_id=tenant_id,
            candidates=candidates,
            failover=failover,
            system_text=system_text,
            canary=canary,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            deadline=time.monotonic() + PURPOSE_DEADLINE_SECONDS[req.purpose],
            request_cap=request_cap,
            input_chars=input_chars,
            untrusted_blocks=untrusted_blocks,
            fingerprint=guards.content_fingerprint(_canonical([m.model_dump() for m in req.messages])),
        )

    async def _budget_exceeded_event(self, tenant_id: uuid.UUID, budget: dict[str, Any]) -> None:
        key = f"aigw:budget_event:{tenant_id}:{budget['period']}"
        if self.redis is not None and not await self.redis.set(key, "1", nx=True, ex=35 * 24 * 3600):
            return
        async with self.app.state.db.tenant_session(tenant_id) as session:
            await enqueue(
                session,
                outbox,
                make_event(
                    "ai.budget.exceeded",
                    producer=PRODUCER,
                    actor=ActorRef(type="system", id=PRODUCER),
                    correlation_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    payload={
                        "scope": "tenant",
                        "period": budget["period"],
                        "limit_micros": budget["tenant_limit"],
                        "used_micros": budget["used_tenant"],
                    },
                ),
            )

    async def _granted_tools(self, req: ResponseRequest, ctx: InternalContext, state: RunState) -> dict[str, ToolSpec]:
        if not req.tool_ids:
            return {}
        if req.agent_id is None:
            raise PolicyDenied("tools_require_agent", "Tools can only be used by a registered agent")
        registry = service_client(self.app, "mcp-registry")
        try:
            granted = await registry.get_json(
                f"/internal/v1/agents/{req.agent_id}/tools", tenant_id=ctx.tenant_id, actor=ctx.actor
            )
        except (UpstreamError, UpstreamUnavailable) as exc:
            raise ServiceUnavailable("Tool registry is unavailable; failing closed") from exc
        by_id = {t["tool_id"]: t for t in granted.get("tools", []) if isinstance(t, dict)}
        tools: dict[str, ToolSpec] = {}
        for tool_id in req.tool_ids:
            spec = by_id.get(tool_id)
            if spec is None:
                state.event("tool_not_granted", "low", "LLM03", f"requested tool {tool_id} is not granted", tool_id)
                continue
            if int(spec.get("risk_class", 4)) >= 4:  # privileged tools are never exposed to agents
                state.event("privileged_tool_blocked", "high", "LLM03", f"class 4 tool {tool_id}", tool_id)
                continue
            schema = spec.get("input_schema") or {"type": "object", "properties": {}, "additionalProperties": False}
            name = provider_tool_name(tool_id)
            tools[name] = ToolSpec(
                tool_id, name, str(spec.get("description", ""))[:1000], schema, int(spec.get("risk_class", 4))
            )
        return tools

    def _assemble(self, req: ResponseRequest, nonce: str, state: RunState) -> tuple[list[dict[str, Any]], int]:
        redact_pii = req.data_classification in self.settings.pii_redact_classifications
        messages: list[dict[str, Any]] = []
        untrusted = 0
        for msg in req.messages:
            parts: list[str] = []
            for block in msg.content:
                text, report = guards.normalize_text(block.text)
                text, secrets_found = guards.redact_secrets(text)
                if secrets_found:
                    state.flag("input_secret_redacted")
                    state.event("secret_in_input", "medium", "LLM02", ",".join(secrets_found))
                if block.effective_trust == "untrusted" and msg.role == "user":
                    untrusted += 1
                    signals = guards.detect_injection_signals(text, report)
                    if signals:
                        state.flag("prompt_injection_suspected")
                        state.event(
                            "prompt_injection_suspected",
                            "medium",
                            "LLM01",
                            f"source={block.source} signals={','.join(signals)}",
                        )
                    if redact_pii:
                        redaction = guards.redact_pii(text)
                        if redaction.counts:
                            state.flag("pii_minimized")
                        text = redaction.text
                    parts.append(guards.wrap_untrusted(text, source=block.source, nonce=nonce))
                else:
                    parts.append(text)
            content = "\n\n".join(parts)
            if messages and messages[-1]["role"] == msg.role and isinstance(messages[-1]["content"], str):
                messages[-1]["content"] += "\n\n" + content
            else:
                messages.append({"role": msg.role, "content": content})
        if messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": "Continue."})
        return messages, untrusted

    # ------------------------------------------------------------------ provider calls with failover
    async def _call(
        self, prep: Prepared, state: RunState, messages: list[dict[str, Any]], *, schema: dict[str, Any] | None
    ) -> ProviderResponse:
        last_error: ProviderError | None = None
        for model in prep.candidates:
            breaker = self.breaker(model.provider)
            if await breaker.state() == "open":
                state.event("provider_circuit_open", "info", "LLM06", model.provider)
                last_error = ProviderTransientError("circuit_open")
                if prep.failover:
                    continue
                break
            remaining = prep.deadline - time.monotonic()
            if remaining <= 1:
                raise PolicyDenied("deadline_exceeded", "The request exceeded its time limit", status=504)
            credentials = await self.app.state.credentials.resolve(
                prep.tenant_id, model.provider, actor=prep.ctx.actor, allow_platform_default=True
            )
            api_key = credentials.values.get("api_key") or credentials.values.get("fake_secret")
            provider = self.app.state.providers[model.provider]
            preq = ProviderRequest(
                model=model.model_id,
                system=prep.system_text,
                messages=messages,
                max_tokens=prep.max_tokens,
                tools=[
                    {"name": t.provider_name, "description": t.description, "input_schema": t.input_schema}
                    for t in prep.tools.values()
                ],
                response_schema=schema,
                effort="low" if prep.req.purpose in ("voice_turn", "classification") else None,
                timeout_seconds=max(1.0, min(remaining, self.settings.anthropic_timeout_seconds)),
            )
            try:
                async with asyncio.timeout(remaining):
                    response = await provider.complete(api_key.get_secret_value() if api_key else "", preq)
            except (TimeoutError, ProviderTransientError) as exc:
                await breaker.record_failure()
                last_error = exc if isinstance(exc, ProviderTransientError) else ProviderTransientError("timeout")
                if prep.failover:
                    state.flag("provider_failover")
                    continue
                break
            except ProviderPermanentError:
                raise ServiceUnavailable("The model provider rejected the request") from None
            await breaker.record_success()
            state.model = model
            self._meter(state, model, response)
            return response
        raise ServiceUnavailable(f"No model provider is available ({last_error.code if last_error else 'none'})")

    def _meter(self, state: RunState, model: ModelInfo, response: ProviderResponse) -> None:
        state.rounds += 1
        state.input_tokens += response.input_tokens
        state.output_tokens += response.output_tokens
        state.cost += cost_micros(response.input_tokens, response.output_tokens, model.in_price, model.out_price)

    # ------------------------------------------------------------------ tools (step 7 and 9)
    async def _run_tools(
        self, prep: Prepared, state: RunState, response: ProviderResponse, seen: dict[str, int]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Returns tool_result blocks and whether the loop must stop."""
        s = self.settings
        results: list[dict[str, Any]] = []
        stop = False
        for i, use in enumerate(response.tool_uses):
            spec = prep.tools.get(use.name)
            tool_id = spec.tool_id if spec else use.name
            if i >= s.max_tool_calls_per_round or len(state.tool_calls) >= s.max_tool_calls_total:
                state.tool_calls.append(ToolCallOut(id=use.id, tool_id=tool_id, status="skipped"))
                results.append(
                    self._tool_result(use.id, "Tool call limit reached; this call was not performed.", is_error=True)
                )
                state.flag("tool_call_limit")
                stop = True
                continue
            if spec is None:
                # The model asked for a tool it was never offered (hallucination or injection).
                state.tool_calls.append(ToolCallOut(id=use.id, tool_id=tool_id[:80], status="not_granted"))
                state.event(
                    "ungranted_tool_call",
                    "high",
                    "LLM03",
                    f"model proposed unavailable tool {use.name[:80]}",
                    use.name[:80],
                )
                results.append(
                    self._tool_result(use.id, "This tool is not available. Nothing was performed.", is_error=True)
                )
                continue
            try:
                jsonschema.validate(use.input, spec.input_schema)
            except jsonschema.ValidationError as exc:
                state.tool_calls.append(ToolCallOut(id=use.id, tool_id=tool_id, status="invalid_arguments"))
                state.event("invalid_tool_arguments", "low", "LLM10", f"{tool_id}: {exc.message[:200]}", tool_id)
                results.append(self._tool_result(use.id, "Invalid arguments; nothing was performed.", is_error=True))
                continue
            key = f"{tool_id}:{_canonical(use.input)}"
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > s.max_repeated_tool_call:
                state.tool_calls.append(ToolCallOut(id=use.id, tool_id=tool_id, status="skipped"))
                state.event("tool_loop_detected", "medium", "LLM06", tool_id, tool_id)
                state.flag("tool_loop_detected")
                results.append(self._tool_result(use.id, "Repeated identical call; stopped.", is_error=True))
                stop = True
                continue
            results.append(await self._execute_tool(prep, state, use.id, spec, use.input))
        return results, stop

    @staticmethod
    def _tool_result(tool_use_id: str, content: str, *, is_error: bool = False) -> dict[str, Any]:
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        if is_error:
            block["is_error"] = True
        return block

    async def _execute_tool(
        self, prep: Prepared, state: RunState, tool_use_id: str, spec: ToolSpec, arguments: Any
    ) -> dict[str, Any]:
        executor = service_client(self.app, "tool-execution", timeout=20.0)
        body = {
            "tool_id": spec.tool_id,
            "arguments": arguments,
            "agent_id": str(prep.req.agent_id),
            "context": prep.req.context.model_dump(mode="json", exclude_none=True),
            "idempotency_key": f"{state.request_id}:{tool_use_id}",
        }
        try:
            result = await executor.post_json(
                "/internal/v1/tools/execute",
                body,
                tenant_id=prep.tenant_id,
                actor=prep.ctx.actor,
                idempotency_key=f"{state.request_id}:{tool_use_id}",
            )
        except (UpstreamError, UpstreamUnavailable):
            state.tool_calls.append(ToolCallOut(id=tool_use_id, tool_id=spec.tool_id, status="failed"))
            return self._tool_result(
                tool_use_id, "The tool failed. Nothing can be assumed to have happened.", is_error=True
            )
        status = str(result.get("status", "failed"))
        if status == "executed":
            state.tool_calls.append(ToolCallOut(id=tool_use_id, tool_id=spec.tool_id, status="executed"))
            raw = _canonical(result.get("result"))[: self.settings.max_tool_result_chars]
            text, report = guards.normalize_text(raw)
            text, secrets_found = guards.redact_secrets(text)
            if secrets_found:
                state.event("secret_in_tool_output", "high", "LLM02", ",".join(secrets_found), spec.tool_id)
            signals = guards.detect_injection_signals(text, report)
            if signals:
                state.flag("tool_output_injection_suspected")
                state.event("tool_output_injection_suspected", "medium", "LLM01", ",".join(signals), spec.tool_id)
            return self._tool_result(tool_use_id, guards.wrap_untrusted(text, source="tool", nonce=guards.new_nonce()))
        if status == "approval_required":
            approval_id = str(result.get("approval_id", ""))[:64] or None
            state.tool_calls.append(
                ToolCallOut(id=tool_use_id, tool_id=spec.tool_id, status="approval_required", approval_id=approval_id)
            )
            return self._tool_result(
                tool_use_id,
                "This action needs approval by a person. It has NOT been performed. "
                "Tell the user it is pending review.",
            )
        state.tool_calls.append(
            ToolCallOut(id=tool_use_id, tool_id=spec.tool_id, status="denied" if status == "denied" else "failed")
        )
        if status == "denied":
            state.event("tool_denied_by_policy", "medium", "LLM03", str(result.get("reason", ""))[:200], spec.tool_id)
        return self._tool_result(
            tool_use_id, "This action was not permitted. It has NOT been performed.", is_error=True
        )

    # ------------------------------------------------------------------ the loop (non-streaming)
    async def run(self, prep: Prepared, state: RunState) -> tuple[str, Any, str]:
        messages = list(prep.messages)
        seen: dict[str, int] = {}
        schema = prep.req.response_schema if not prep.tools else None
        stop_reason = "end_turn"
        response: ProviderResponse | None = None
        for _round in range(self.settings.max_tool_rounds + 1):
            response = await self._call(prep, state, messages, schema=schema)
            stop_reason = response.stop_reason
            if state.cost > prep.request_cap:
                state.flag("request_cost_cap")
                stop_reason = "cost_limit"
                break
            if not response.tool_uses:
                break
            if not prep.tools:
                # Tool calls without any granted tool (hallucination or injection): never forwarded.
                for use in response.tool_uses:
                    state.tool_calls.append(ToolCallOut(id=use.id, tool_id=use.name[:80], status="not_granted"))
                    state.event(
                        "ungranted_tool_call",
                        "high",
                        "LLM03",
                        f"model proposed unavailable tool {use.name[:80]}",
                        use.name[:80],
                    )
                stop_reason = "tool_not_available"
                break
            results, stop = await self._run_tools(prep, state, response, seen)
            messages = [
                *messages,
                {"role": "assistant", "content": response.raw_content},
                {"role": "user", "content": results},
            ]
            if stop:
                stop_reason = "tool_limit"
                break
        else:
            stop_reason = "tool_limit"
            state.flag("tool_round_limit")
        assert response is not None
        text = response.text
        text = self._check_output(prep, state, text)
        parsed: Any = None
        if prep.req.response_schema is not None and text != BLOCKED_OUTPUT_TEXT:
            parsed, text = await self._validated_json(prep, state, messages, text)
        return text, parsed, stop_reason

    def _check_output(self, prep: Prepared, state: RunState, text: str) -> str:
        leak = guards.leaks_hidden_context(text, canary=prep.canary, system_text=prep.req.system)
        if leak:
            state.flag("output_blocked")
            state.event("hidden_context_leak_blocked", "high", "LLM08", leak)
            return BLOCKED_OUTPUT_TEXT
        clean, report = guards.sanitize_output(text)
        for flag in report.flags:
            state.flag(flag)
        if report.secrets_redacted:
            state.event("secret_in_output_redacted", "high", "LLM02", ",".join(report.secrets_redacted))
        if report.images_removed or report.active_html_removed or report.dangerous_links:
            state.event("unsafe_output_neutralized", "medium", "LLM10", ",".join(report.flags))
        return clean

    async def _validated_json(
        self, prep: Prepared, state: RunState, messages: list[dict[str, Any]], text: str
    ) -> tuple[Any, str]:
        schema = prep.req.response_schema
        assert schema is not None
        error = ""
        for attempt in range(self.settings.schema_repair_attempts + 1):
            try:
                value = _extract_json(text)
                jsonschema.validate(value, schema)
                return value, text
            except (ValueError, jsonschema.ValidationError) as exc:
                error = exc.message if isinstance(exc, jsonschema.ValidationError) else "not valid JSON"
                state.flag("schema_repair")
                if attempt >= self.settings.schema_repair_attempts:
                    break
                repair = [
                    *messages,
                    {"role": "assistant", "content": text or "(empty)"},
                    {
                        "role": "user",
                        "content": "Your previous answer did not match the required JSON schema ("
                        f"{error[:200]}). Reply again with only the JSON object.",
                    },
                ]
                response = await self._call(prep, state, repair, schema=schema)
                text = self._check_output(prep, state, response.text)
        state.event("invalid_model_output", "low", "LLM10", error[:200])
        raise PolicyDenied("invalid_model_output", "The model output did not match the required schema", status=502)

    # ------------------------------------------------------------------ streaming
    async def stream(self, prep: Prepared, state: RunState) -> AsyncIterator[dict[str, Any]]:
        """Text deltas are released sentence by sentence, after the leak check on everything so far,
        so a hidden-context leak is stopped before it reaches the caller (holdback buffering)."""
        messages = list(prep.messages)
        seen: dict[str, int] = {}
        emitted_raw = ""
        stop_reason = "end_turn"
        for _round in range(self.settings.max_tool_rounds + 1):
            model = prep.candidates[0]
            credentials = await self.app.state.credentials.resolve(
                prep.tenant_id, model.provider, actor=prep.ctx.actor, allow_platform_default=True
            )
            api_key = credentials.values.get("api_key") or credentials.values.get("fake_secret")
            breaker = self.breaker(model.provider)
            if await breaker.state() == "open":
                yield {"type": "error", "code": "provider_unavailable"}
                return
            preq = ProviderRequest(
                model=model.model_id,
                system=prep.system_text,
                messages=messages,
                max_tokens=prep.max_tokens,
                tools=[
                    {"name": t.provider_name, "description": t.description, "input_schema": t.input_schema}
                    for t in prep.tools.values()
                ],
                effort="low" if prep.req.purpose in ("voice_turn", "classification") else None,
                timeout_seconds=max(1.0, prep.deadline - time.monotonic()),
            )
            buffer = ""
            final: ProviderResponse | None = None
            try:
                async with asyncio.timeout(max(1.0, prep.deadline - time.monotonic())):
                    async for ev in self.app.state.providers[model.provider].stream(
                        api_key.get_secret_value() if api_key else "", preq
                    ):
                        if ev.type == "text_delta":
                            buffer += ev.text
                            cut = max(buffer.rfind(". "), buffer.rfind("\n"), buffer.rfind("? "), buffer.rfind("! "))
                            if cut < 0 and len(buffer) < 240:
                                continue
                            segment, buffer = (buffer[: cut + 1], buffer[cut + 1 :]) if cut >= 0 else (buffer, "")
                            if guards.leaks_hidden_context(
                                emitted_raw + segment, canary=prep.canary, system_text=prep.req.system
                            ):
                                state.flag("output_blocked")
                                state.event("hidden_context_leak_blocked", "high", "LLM08", "stream")
                                yield {"type": "error", "code": "output_blocked"}
                                return
                            emitted_raw += segment
                            clean = self._check_output(prep, state, segment)
                            yield {"type": "text_delta", "text": clean}
                        elif ev.type == "final":
                            final = ev.response
            except (TimeoutError, ProviderTransientError):
                await breaker.record_failure()
                yield {"type": "error", "code": "provider_unavailable"}
                return
            except ProviderPermanentError:
                yield {"type": "error", "code": "provider_rejected"}
                return
            await breaker.record_success()
            if buffer:
                if guards.leaks_hidden_context(emitted_raw + buffer, canary=prep.canary, system_text=prep.req.system):
                    state.flag("output_blocked")
                    state.event("hidden_context_leak_blocked", "high", "LLM08", "stream")
                    yield {"type": "error", "code": "output_blocked"}
                    return
                emitted_raw += buffer
                yield {"type": "text_delta", "text": self._check_output(prep, state, buffer)}
            assert final is not None
            state.model = model
            self._meter(state, model, final)
            stop_reason = final.stop_reason
            if state.cost > prep.request_cap:
                state.flag("request_cost_cap")
                stop_reason = "cost_limit"
                break
            if not final.tool_uses:
                break
            results, stop = await self._run_tools(prep, state, final, seen)
            for call in state.tool_calls[-len(final.tool_uses) :]:
                yield {"type": "tool_call", "tool_id": call.tool_id, "status": call.status}
            messages = [
                *messages,
                {"role": "assistant", "content": final.raw_content},
                {"role": "user", "content": results},
            ]
            if stop:
                stop_reason = "tool_limit"
                break
        yield {
            "type": "done",
            "request_id": str(state.request_id),
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": state.input_tokens,
                "output_tokens": state.output_tokens,
                "cost_micros": state.cost,
            },
            "flags": state.flags,
        }

    # ------------------------------------------------------------------ concurrency + persistence (step 11)
    @contextlib.asynccontextmanager
    async def slot(self, tenant_id: uuid.UUID, request_id: uuid.UUID, ttl: int) -> AsyncIterator[None]:
        key = f"aigw:concurrency:{tenant_id}"
        if not await acquire_slot(
            self.redis, key, str(request_id), max_slots=self.settings.tenant_max_concurrent, ttl_seconds=ttl
        ):
            raise PolicyDenied("concurrency_limit", "Too many concurrent AI requests for this tenant", status=429)
        try:
            yield
        finally:
            await release_slot(self.redis, key, str(request_id))

    async def persist(
        self,
        *,
        req: ResponseRequest | EmbeddingsRequest,
        ctx: InternalContext,
        state: RunState,
        status: str,
        deny_reason: str | None,
        stop_reason: str | None,
        prep: Prepared | None,
        purpose: str,
    ) -> None:
        tenant_id = ctx.tenant_id
        if tenant_id is None:
            return
        agent_id = getattr(req, "agent_id", None)
        latency = int((time.monotonic() - state.started) * 1000)
        classification = req.data_classification
        try:
            async with self.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
                await session.execute(
                    insert(ai_requests).values(
                        id=state.request_id,
                        tenant_id=tenant_id,
                        caller=str(ctx.principal.azp or "unknown")[:80],
                        actor=str(ctx.actor)[:160],
                        agent_id=agent_id,
                        purpose=purpose,
                        classification=classification,
                        model=state.model.model_id if state.model else None,
                        provider=state.model.provider if state.model else None,
                        status=status,
                        deny_reason=deny_reason,
                        stop_reason=stop_reason,
                        rounds=state.rounds,
                        tool_calls=[c.model_dump(exclude_none=True) for c in state.tool_calls],
                        input_chars=prep.input_chars if prep else 0,
                        untrusted_blocks=prep.untrusted_blocks if prep else 0,
                        input_tokens=state.input_tokens,
                        output_tokens=state.output_tokens,
                        cost_micros=state.cost,
                        latency_ms=latency,
                        flags=state.flags,
                        input_fingerprint=prep.fingerprint if prep else None,
                        correlation_id=None,
                    )
                )
                await record_usage(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    input_tokens=state.input_tokens,
                    output_tokens=state.output_tokens,
                    cost=state.cost,
                    denied=status == "denied",
                )
                for ev in state.events:
                    await session.execute(
                        insert(ai_security_events).values(
                            tenant_id=tenant_id,
                            request_id=state.request_id,
                            kind=ev.kind,
                            severity=ev.severity,
                            owasp=ev.owasp,
                            agent_id=agent_id,
                            tool_id=ev.tool_id,
                            detail=ev.detail,
                        )
                    )
                    if ev.severity in ("medium", "high"):
                        await enqueue(
                            session,
                            outbox,
                            make_event(
                                "ai.security_event",
                                producer=PRODUCER,
                                actor=ActorRef(type="system", id=PRODUCER),
                                correlation_id=uuid.uuid4(),
                                tenant_id=tenant_id,
                                payload={
                                    "kind": ev.kind,
                                    "agent_id": str(agent_id) if agent_id else None,
                                    "tool_id": ev.tool_id,
                                    "detail": f"{ev.owasp}: {ev.detail}"[:500],
                                },
                            ),
                        )
                if state.model is not None:
                    await enqueue(
                        session,
                        outbox,
                        make_event(
                            "ai.request.completed",
                            producer=PRODUCER,
                            actor=ActorRef(type="system", id=PRODUCER),
                            correlation_id=uuid.uuid4(),
                            tenant_id=tenant_id,
                            payload={
                                "request_id": str(state.request_id),
                                "model": state.model.model_id,
                                "provider": state.model.provider,
                                "input_tokens": state.input_tokens,
                                "output_tokens": state.output_tokens,
                                "cost_micros": state.cost,
                                "latency_ms": latency,
                                "purpose": purpose,
                            },
                        ),
                    )
        except Exception:
            log.exception("ai_request_persist_failed", extra={"request_id": str(state.request_id)})

    # ------------------------------------------------------------------ embeddings
    async def embed(
        self, req: EmbeddingsRequest, ctx: InternalContext, state: RunState
    ) -> tuple[list[list[float]], str]:
        s = self.settings
        tenant_id = ctx.require_tenant()
        tenant_client = service_client(self.app, "tenant-service")
        try:
            ai_settings = await tenant_settings_section(tenant_client, self.redis, tenant_id, "ai")
        except (UpstreamError, UpstreamUnavailable) as exc:
            raise ServiceUnavailable("Tenant AI policy is unavailable; failing closed") from exc
        if not ai_settings.get("effective_ai_enabled", False):
            raise PolicyDenied("ai_disabled", "AI is disabled for this tenant or platform")
        check_classification(req.data_classification, str(ai_settings.get("data_classification_max", "internal")))
        async with self.app.state.db.tenant_session(tenant_id, ctx.actor) as session:
            reason = await kill_switch_reason(session, tenant_id=tenant_id, agent_id=None)
            if reason:
                raise PolicyDenied(reason, "An AI kill switch is engaged")
            models, _ = await resolve_models(
                session,
                tenant_id=tenant_id,
                purpose="embedding",
                requested=None,
                classification=req.data_classification,
                tenant_allowed=[s.embedding_model],
                agent_id=None,
            )
            budget = await budget_state(
                session,
                agent_id=None,
                tenant_budget_usd=int(ai_settings.get("monthly_budget_usd", 0)),
                default_request_cap=s.default_request_cost_cap_micros,
            )
        if not await allow_fixed_window(
            self.redis, f"aigw:rpm:{tenant_id}:{int(time.time() // 60)}", s.tenant_requests_per_minute, 70
        ):
            raise PolicyDenied("rate_limited", "Tenant AI request rate exceeded", status=429)
        model = models[0]
        chars = sum(len(t) for t in req.texts)
        check_budget(budget, cost_micros(chars // 3 + 50, 0, model.in_price, 0))
        texts = []
        for text in req.texts:  # secrets are never embedded (LLM02 / embedding inversion)
            clean, _ = guards.normalize_text(text)
            clean, found = guards.redact_secrets(clean)
            if found:
                state.event("secret_in_embedding_input", "medium", "LLM02", ",".join(found))
            texts.append(clean)
        creds = await self.app.state.credentials.resolve(
            tenant_id, model.provider, actor=ctx.actor, allow_platform_default=True
        )
        api_key = creds.values.get("api_key") or creds.values.get("fake_secret")
        breaker = self.breaker(model.provider)
        if await breaker.state() == "open":
            raise ServiceUnavailable("Embedding provider is unavailable")
        try:
            vectors = await self.app.state.embeddings.embed(
                api_key.get_secret_value() if api_key else "", model.model_id, texts, s.embedding_dims
            )
        except ProviderTransientError:
            await breaker.record_failure()
            raise ServiceUnavailable("Embedding provider is unavailable") from None
        except ProviderPermanentError:
            raise ServiceUnavailable("The embedding provider rejected the request") from None
        await breaker.record_success()
        state.model = model
        state.rounds = 1
        state.input_tokens = chars // 4
        state.cost = cost_micros(state.input_tokens, 0, model.in_price, 0)
        return vectors, model.model_id
