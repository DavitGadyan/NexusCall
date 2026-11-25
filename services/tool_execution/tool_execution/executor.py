"""The tool execution pipeline: the security boundary that decides whether an AI-proposed call may run.

A model proposing a call authorizes nothing (build.md §24.4). Every call goes through, in order:

  1. grant check with mcp-registry: unknown, disabled, revoked, ungranted and class-4 tools are denied
  2. argument validation against the registered input schema; unknown fields and any identity, authority
     or credential field are rejected (independently of the schema)
  3. resource binding from the VERIFIED context: contact/call ids never come from arguments, and a call's
     contact must match the context's contact (checked with call-service)
  4. per-agent, per-tool and per-MCP-server rate limits (and MCP concurrency slots + circuit breaker)
  5. risk policy: class 0–2 execute (1 = audited sensitive read, 2 = idempotency key forwarded);
     class 3 (or anything tightened to "approval") needs an approved, unconsumed, unexpired row in
     `ai_approval_requests` for the same tool, args hash and context hash; class 4 never runs
  6. execution: internal tools through `service_client` (tenant, actor, idempotency headers); MCP tools
     through the pinned, allowlisted streamable-HTTP client, the Vault credential read only now
  7. output projection, schema validation and size cap
  8. an append-only record plus audit/security events, without arguments, outputs or secrets
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from jsonschema import Draft202012Validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_core.circuit import CircuitBreaker
from nexus_core.clients import UpstreamError, UpstreamUnavailable, service_client
from nexus_core.config import Environment
from nexus_core.context import Actor, correlation_id_var, request_id_var
from nexus_core.idempotency import canonical_hash
from nexus_core.ratelimit import acquire_slot, allow_fixed_window, release_slot
from nexus_core.realtime import publish_ui_event
from nexus_core.vault import VaultError
from tool_execution.adapters import ADAPTERS, ArgumentRejected, InternalAdapter
from tool_execution.events import APPROVER_PERMISSION, audit, notify_approvers, security_event
from tool_execution.mcp_client import McpError, McpIdentityMismatch, McpTarget
from tool_execution.settings import Settings
from tool_execution.tables import ai_approval_requests, approval_tenants, tool_execution_records

log = logging.getLogger(__name__)

TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
# Never accepted inside model arguments, at any depth, whatever a schema says (build.md §24.4).
FORBIDDEN_KEYS = frozenset(
    {
        "tenant", "tenant_id", "user", "user_id", "actor", "actor_id", "agent_id", "role", "roles",
        "permission", "permissions", "scope", "scopes", "contact_id", "call_id", "thread_id", "approved",
        "approval", "approval_id", "approved_by", "credential", "credentials", "token", "access_token",
        "api_key", "secret", "password", "authorization", "headers", "url", "endpoint", "vault_ref",
    }
)  # fmt: skip
REQUIRED_SPEC = ("granted", "enabled", "risk_class", "requires_approval", "version", "input_schema", "output_schema",
                 "target", "limits")  # fmt: skip
STATUS_AUDIT = {
    "executed": ("ai.tool.executed", "success"),
    "approval_required": ("ai.tool.approval_required", "success"),
    "denied": ("ai.tool.authorization_denied", "denied"),
    "failed": ("ai.tool.failed", "failure"),
}


@dataclass(frozen=True, slots=True)
class ToolCall:
    tenant_id: uuid.UUID
    actor: Actor
    caller: str
    agent_id: uuid.UUID
    tool_id: str
    arguments: Any
    context: dict[str, str]
    idempotency_key: str


@dataclass(slots=True)
class Outcome:
    status: str
    result: Any = None
    approval_id: uuid.UUID | None = None
    reason: str | None = None
    transient: bool = False

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.status == "executed":
            out["result"] = self.result
        if self.approval_id is not None:
            out["approval_id"] = str(self.approval_id)
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass(slots=True)
class Trace:
    record_id: uuid.UUID = field(default_factory=uuid.uuid4)
    spec: dict[str, Any] | None = None
    args_hash: str | None = None
    bound: dict[str, str] = field(default_factory=dict)
    server_id: uuid.UUID | None = None
    output_bytes: int = 0
    approval_id: uuid.UUID | None = None
    sensitive_read: bool = False
    security: list[tuple[str, str]] = field(default_factory=list)


class Denied(Exception):
    def __init__(
        self,
        reason: str,
        security: str | None = None,
        detail: str = "",
        *,
        status: str = "denied",
        transient: bool = False,
    ) -> None:
        super().__init__(reason)
        self.reason, self.security, self.detail, self.status, self.transient = (
            reason,
            security,
            detail,
            status,
            transient,
        )


def find_forbidden_keys(value: Any, depth: int = 0) -> set[str]:
    found: set[str] = set()
    if depth > 16:
        return {"<too_deep>"}
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                found.add(str(k)[:40])
            found |= find_forbidden_keys(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            found |= find_forbidden_keys(v, depth + 1)
    return found


def _scrub(value: Any, secret: str) -> tuple[Any, bool]:
    """Remove a credential a server echoed back. Returns (value, found)."""
    if not secret:
        return value, False
    if isinstance(value, str):
        return (value.replace(secret, "[REDACTED]"), True) if secret in value else (value, False)
    if isinstance(value, dict):
        out, hit = {}, False
        for k, v in value.items():
            out[k], h = _scrub(v, secret)
            hit = hit or h
        return out, hit
    if isinstance(value, list):
        items = [_scrub(v, secret) for v in value]
        return [v for v, _ in items], any(h for _, h in items)
    return value, False


class Executor:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    @property
    def settings(self) -> Settings:
        return self.app.state.settings  # type: ignore[no-any-return]

    @property
    def redis(self) -> Any:
        return getattr(self.app.state, "redis", None)

    # ------------------------------------------------------------------ entry points
    async def execute(
        self, call: ToolCall, *, finalize: Callable[[AsyncSession, Outcome], Awaitable[None]] | None = None
    ) -> Outcome:
        trace = Trace()
        started = time.monotonic()
        try:
            outcome = await self._pipeline(call, trace)
        except Denied as d:
            outcome = self._denied(d, trace)
        except Exception:
            log.exception("tool_pipeline_error", extra={"tool_id": call.tool_id[:80]})
            outcome = Outcome("failed", reason="internal_error", transient=True)
        await self._record(call, trace, outcome, started, finalize)
        return outcome

    async def run_approved(self, tenant_id: uuid.UUID, approval: dict[str, Any], approver: Actor) -> Outcome:
        """Run a CLAIMED (consumed) approval exactly once, from the stored canonical arguments.

        Everything is re-checked: the grant may have been revoked and the context must still bind."""
        call = ToolCall(
            tenant_id=tenant_id,
            actor=approver,
            caller=approval["caller"],
            agent_id=approval["agent_id"],
            tool_id=approval["tool_id"],
            arguments=approval["arguments"],
            context=dict(approval["context"]),
            idempotency_key=f"approval:{approval['id']}",
        )
        trace = Trace(approval_id=approval["id"])
        started = time.monotonic()
        try:
            spec, args, adapter = await self._authorize(call, trace)
            bound = await self._bind(call, adapter.needs if adapter else frozenset())
            trace.bound = bound
            if canonical_hash(bound) != approval["context_hash"] or trace.args_hash != approval["args_hash"]:
                raise Denied("approval_context_changed", "approval_context_mismatch")
            outcome = await self._run(call, spec, adapter, args, bound, trace)
        except Denied as d:
            outcome = self._denied(d, trace)
        except Exception:
            log.exception("approved_tool_error", extra={"approval_id": str(approval["id"])})
            outcome = Outcome("failed", reason="internal_error")
        outcome.approval_id = approval["id"]

        async def finalize(s: AsyncSession, o: Outcome) -> None:
            await s.execute(
                update(ai_approval_requests)
                .where(ai_approval_requests.c.id == approval["id"])
                .values(
                    execution_record_id=trace.record_id,
                    result_status=o.status,
                    result_reason=o.reason,
                    updated_at=func.now(),
                )
            )

        await self._record(call, trace, outcome, started, finalize)
        return outcome

    # ------------------------------------------------------------------ pipeline
    async def _pipeline(self, call: ToolCall, trace: Trace) -> Outcome:
        spec, args, adapter = await self._authorize(call, trace)
        bound = await self._bind(call, adapter.needs if adapter else frozenset())
        trace.bound = bound
        await self._limits(call, spec, trace)
        if spec["requires_approval"] or int(spec["risk_class"]) >= 3:
            return await self._approval_gate(call, spec, args, bound, trace)
        return await self._run(call, spec, adapter, args, bound, trace)

    async def _authorize(
        self, call: ToolCall, trace: Trace
    ) -> tuple[dict[str, Any], dict[str, Any], InternalAdapter | None]:
        if len(call.tool_id) > 80 or not TOOL_ID_RE.match(call.tool_id):
            raise Denied("unknown_tool", "unregistered_tool", "tool id is not a registered identifier")
        spec = await self._grant_check(call)
        trace.spec = spec
        risk = int(spec["risk_class"])
        if risk >= 4:
            raise Denied("privileged_tool", "privileged_tool_denied", "class 4 tools never run for agents")
        if not spec["enabled"]:
            raise Denied(str(spec.get("reason") or "tool_disabled")[:64], "tool_authorization_denied", "disabled")
        if not spec["granted"]:
            raise Denied(str(spec.get("reason") or "not_granted")[:64], "tool_authorization_denied", "not granted")
        args = self._validate_args(call.arguments, spec, trace)
        target = spec["target"]
        adapter: InternalAdapter | None = None
        if target.get("kind") == "internal":
            adapter = ADAPTERS.get(call.tool_id)
            if adapter is None or not adapter.matches(target):
                raise Denied("target_mismatch", "tool_target_mismatch", "registry target differs from code")
        elif target.get("kind") == "mcp":
            try:
                trace.server_id = uuid.UUID(str(target["server_id"]))
            except (KeyError, ValueError) as exc:
                raise Denied("target_mismatch", "tool_target_mismatch", "mcp target incomplete") from exc
        else:
            raise Denied("target_mismatch", "tool_target_mismatch", "unknown target kind")
        return spec, args, adapter

    async def _grant_check(self, call: ToolCall) -> dict[str, Any]:
        registry = service_client(self.app, "mcp-registry")
        try:
            spec = await registry.post_json(
                f"/internal/v1/tools/{call.tool_id}/grant-check",
                {"agent_id": str(call.agent_id)},
                tenant_id=call.tenant_id,
                actor=call.actor,
            )
        except UpstreamError as exc:
            if exc.is_not_found:
                raise Denied("unknown_tool", "unregistered_tool", "tool is not registered") from exc
            raise Denied("policy_unavailable", transient=True) from exc
        except UpstreamUnavailable as exc:  # fail closed
            raise Denied("policy_unavailable", transient=True) from exc
        if not isinstance(spec, dict) or any(k not in spec for k in REQUIRED_SPEC):
            raise Denied("policy_unavailable", transient=True)
        return spec

    def _validate_args(self, arguments: Any, spec: dict[str, Any], trace: Trace) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise Denied("invalid_arguments")
        if len(json.dumps(arguments, default=str)) > self.settings.max_argument_bytes:
            raise Denied("arguments_too_large")
        forbidden = find_forbidden_keys(arguments)
        if forbidden:
            raise Denied(
                "identity_fields_rejected", "identity_fields_in_arguments", "fields: " + ",".join(sorted(forbidden))
            )
        errors = list(Draft202012Validator(spec["input_schema"]).iter_errors(arguments))
        if errors:
            raise Denied("invalid_arguments")
        trace.args_hash = canonical_hash(arguments)
        return arguments

    async def _bind(self, call: ToolCall, needs: frozenset[str]) -> dict[str, str]:
        ctx = call.context
        call_id, contact_id, thread_id = ctx.get("call_id"), ctx.get("contact_id"), ctx.get("thread_id")
        if call_id and needs & {"contact_id", "call_id"}:
            calls = service_client(self.app, "call-service")
            try:
                record = await calls.get_json(
                    f"/internal/v1/calls/{call_id}", tenant_id=call.tenant_id, actor=call.actor
                )
            except UpstreamError as exc:
                if exc.is_not_found:
                    raise Denied("context_invalid", "context_binding_failed", "call not found in tenant") from exc
                raise Denied("context_unavailable", transient=True) from exc
            except UpstreamUnavailable as exc:
                raise Denied("context_unavailable", transient=True) from exc
            call_contact = record.get("contact_id") if isinstance(record, dict) else None
            if str(record.get("call_id", call_id)) != call_id:
                raise Denied("context_invalid", "context_binding_failed", "call lookup mismatch")
            if contact_id and str(call_contact or "") != contact_id:
                raise Denied("context_mismatch", "context_binding_failed", "contact is not the call's contact")
            contact_id = contact_id or (str(call_contact) if call_contact else None)
        if "contact_id" in needs and not contact_id:
            raise Denied("context_required")
        if "call_id" in needs and not call_id:
            raise Denied("context_required")
        return {k: v for k, v in (("call_id", call_id), ("contact_id", contact_id), ("thread_id", thread_id)) if v}

    async def _limits(self, call: ToolCall, spec: dict[str, Any], trace: Trace) -> None:
        limits = spec["limits"]
        windows = [
            (f"te:rl:agent:{call.tenant_id}:{call.agent_id}", int(limits["agent_calls_per_minute"])),
            (f"te:rl:tool:{call.tenant_id}:{call.agent_id}:{call.tool_id}", int(limits["rate_per_minute"])),
        ]
        if trace.server_id is not None:
            windows.append((f"te:rl:server:{trace.server_id}", int(spec["target"].get("server_rate_per_minute", 60))))
        minute = int(time.time() // 60)
        for key, limit in windows:
            try:
                allowed = await allow_fixed_window(self.redis, f"{key}:{minute}", limit, 60)
            except Exception as exc:
                raise Denied("rate_limit_unavailable", transient=True) from exc
            if not allowed:
                raise Denied("rate_limited", "tool_rate_limited", key.split(":")[2])

    # ------------------------------------------------------------------ approvals (class 3)
    async def _approval_gate(
        self, call: ToolCall, spec: dict[str, Any], args: dict[str, Any], bound: dict[str, str], trace: Trace
    ) -> Outcome:
        db = self.app.state.db
        args_hash, context_hash = trace.args_hash or canonical_hash(args), canonical_hash(bound)
        same = (
            ai_approval_requests.c.tool_id == call.tool_id,
            ai_approval_requests.c.agent_id == call.agent_id,
            ai_approval_requests.c.args_hash == args_hash,
            ai_approval_requests.c.context_hash == context_hash,
        )
        async with db.tenant_session(call.tenant_id, call.actor) as s:
            # A previously approved, not yet used, unexpired approval for exactly this call: claim it once.
            claimed = (
                (
                    await s.execute(
                        update(ai_approval_requests)
                        .where(
                            *same,
                            ai_approval_requests.c.id
                            == select(ai_approval_requests.c.id)
                            .where(
                                *same,
                                ai_approval_requests.c.status == "approved",
                                ai_approval_requests.c.expires_at > func.now(),
                            )
                            .limit(1)
                            .scalar_subquery(),
                            ai_approval_requests.c.status == "approved",
                        )
                        .values(status="consumed", consumed_at=func.now(), updated_at=func.now())
                        .returning(*ai_approval_requests.c)
                    )
                )
                .mappings()
                .first()
            )
            if claimed is None:
                await s.execute(
                    update(ai_approval_requests)
                    .where(
                        *same,
                        ai_approval_requests.c.status == "pending",
                        ai_approval_requests.c.expires_at <= func.now(),
                    )
                    .values(status="expired", updated_at=func.now())
                )
                pending = (
                    await s.execute(
                        select(ai_approval_requests.c.id).where(*same, ai_approval_requests.c.status == "pending")
                    )
                ).scalar_one_or_none()
                if pending is None:
                    pending = (
                        await s.execute(
                            insert(ai_approval_requests)
                            .values(
                                tenant_id=call.tenant_id,
                                tool_id=call.tool_id,
                                tool_version=int(spec["version"]),
                                risk_class=int(spec["risk_class"]),
                                agent_id=call.agent_id,
                                arguments=args,
                                args_hash=args_hash,
                                context=bound,
                                context_hash=context_hash,
                                requested_by_type=call.actor.type,
                                requested_by_id=call.actor.id[:128],
                                caller=call.caller[:128],
                                expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.approval_ttl_seconds),
                            )
                            .returning(ai_approval_requests.c.id)
                        )
                    ).scalar_one()
                    await s.execute(
                        insert(approval_tenants)
                        .values(tenant_id=call.tenant_id)
                        .on_conflict_do_update(index_elements=["tenant_id"], set_={"last_request_at": func.now()})
                    )
                    await notify_approvers(s, call.actor, call.tenant_id, pending)
                    await audit(
                        s, call.actor, "ai.tool.approval_requested", "ai_approval_request", pending,
                        tenant_id=call.tenant_id, tool_id=call.tool_id, agent_id=str(call.agent_id),
                        risk_class=int(spec["risk_class"]),
                    )  # fmt: skip
                    try:
                        await publish_ui_event(
                            self.redis, call.tenant_id, "ai.approval.created", permission=APPROVER_PERMISSION,
                            data={"approval_id": str(pending)},
                        )  # fmt: skip
                    except Exception:  # noqa: BLE001 - UI hint only
                        log.warning("ui_event_failed")
                trace.approval_id = pending
                return Outcome("approval_required", approval_id=pending)
        approval = dict(claimed)
        approver = Actor("user", str(approval["decided_by"]))
        trace.approval_id = approval["id"]
        try:
            result = await self._run(
                ToolCall(call.tenant_id, approver, call.caller, call.agent_id, call.tool_id, args, call.context,
                         f"approval:{approval['id']}"),
                spec, ADAPTERS.get(call.tool_id), args, bound, trace,
            )  # fmt: skip
        except Denied as d:
            result = self._denied(d, trace)
            result.transient = False  # the approval is spent either way; never re-run blindly
        result.approval_id = approval["id"]
        async with db.tenant_session(call.tenant_id, approver) as s:
            await s.execute(
                update(ai_approval_requests)
                .where(ai_approval_requests.c.id == approval["id"])
                .values(execution_record_id=trace.record_id, result_status=result.status, result_reason=result.reason)
            )
        return result

    # ------------------------------------------------------------------ execution
    async def _run(
        self,
        call: ToolCall,
        spec: dict[str, Any],
        adapter: InternalAdapter | None,
        args: dict[str, Any],
        bound: dict[str, str],
        trace: Trace,
    ) -> Outcome:
        if int(spec["risk_class"]) == 1:
            trace.sensitive_read = True
        if adapter is not None:
            result = await self._run_internal(call, spec, adapter, args, bound, trace)
        else:
            result = await self._run_mcp(call, spec, args, trace)
        blob = json.dumps(result, default=str)
        trace.output_bytes = len(blob.encode())
        if trace.output_bytes > int(spec["limits"]["max_output_bytes"]):
            raise Denied("output_too_large", status="failed")
        if list(Draft202012Validator(spec["output_schema"]).iter_errors(result)):
            raise Denied("invalid_output", "tool_output_invalid", "output does not match the schema", status="failed")
        return Outcome("executed", result=result)

    def _idempotency_key(self, call: ToolCall, spec: dict[str, Any]) -> str | None:
        if not spec.get("side_effecting") and int(spec["risk_class"]) < 2:
            return None
        if call.idempotency_key.startswith("approval:"):
            return call.idempotency_key
        return f"tool:{call.tool_id}:{call.idempotency_key}"[:255]

    async def _run_internal(
        self,
        call: ToolCall,
        spec: dict[str, Any],
        adapter: InternalAdapter,
        args: dict[str, Any],
        bound: dict[str, str],
        trace: Trace,
    ) -> Any:
        try:
            req = adapter.build(args, bound, str(trace.record_id))
        except ArgumentRejected as exc:
            raise Denied("invalid_arguments") from exc
        client = service_client(self.app, adapter.service)
        try:
            resp = await client.request(
                adapter.method,
                req.path,
                tenant_id=call.tenant_id,
                actor=call.actor,
                json_body=req.body,
                params=req.params,
                idempotency_key=self._idempotency_key(call, spec),
                timeout_seconds=int(spec["limits"]["timeout_ms"]) / 1000,
            )
        except UpstreamError as exc:
            if exc.is_denied:
                raise Denied("upstream_denied", status="denied") from exc
            reason = {404: "resource_not_found", 409: "conflict", 422: "rejected_by_owner"}.get(
                exc.upstream_status, "upstream_error"
            )
            raise Denied(reason, status="failed", transient=exc.upstream_status >= 500) from exc
        except UpstreamUnavailable as exc:
            raise Denied("upstream_unavailable", status="failed", transient=True) from exc
        try:
            data = resp.json() if resp.content else {}
        except ValueError as exc:
            raise Denied("invalid_output", status="failed") from exc
        return adapter.project(data)

    async def _run_mcp(self, call: ToolCall, spec: dict[str, Any], args: dict[str, Any], trace: Trace) -> Any:
        target = spec["target"]
        server_id = trace.server_id
        assert server_id is not None
        cfg = self.settings
        if await self._quarantined(server_id):
            raise Denied("server_quarantined", status="denied")
        breaker = CircuitBreaker(
            self.redis,
            f"mcp:{server_id}",
            failure_threshold=cfg.mcp_breaker_failures,
            open_seconds=cfg.mcp_breaker_open_seconds,
        )
        if await breaker.state() == "open":
            raise Denied("circuit_open", status="failed", transient=True)
        endpoint = str(target["endpoint"])
        from urllib.parse import urlsplit

        host = (urlsplit(endpoint).hostname or "").lower()
        hosts = frozenset(str(h).lower() for h in target.get("allowed_hosts") or ())
        private = host in cfg.mcp_private_hosts
        schemes = {"https"}
        if private and cfg.mcp_allow_http_private and cfg.env in (Environment.DEVELOPMENT, Environment.TEST):
            schemes.add("http")
        mcp_target = McpTarget(
            endpoint=endpoint,
            allowed_hosts=hosts,
            tool_name=str(target.get("tool_name", "")),
            pinned_server_name=str(target.get("pinned_server_name") or ""),
            pinned_version=str(target.get("pinned_version") or ""),
            timeout_seconds=min(int(target.get("timeout_ms", 5000)), int(spec["limits"]["timeout_ms"])) / 1000,
            max_request_bytes=int(target.get("max_request_bytes", 16384)),
            max_response_bytes=min(
                int(target.get("max_response_bytes", 65536)), int(spec["limits"]["max_output_bytes"])
            ),
            allow_private=private,
            allowed_schemes=frozenset(schemes),
        )
        if not mcp_target.pinned_server_name or not mcp_target.pinned_version or not mcp_target.tool_name:
            raise Denied("server_not_pinned", "tool_target_mismatch", f"server_id={server_id}")
        slot_key, member = f"te:mcp:slots:{server_id}", str(trace.record_id)
        if not await acquire_slot(
            self.redis, slot_key, member, max_slots=int(target.get("max_concurrency", 4)),
            ttl_seconds=int(mcp_target.timeout_seconds * 3) + 5,
        ):  # fmt: skip
            raise Denied("concurrency_limited", "tool_rate_limited", "mcp server concurrency")
        try:
            token = await self._credential(target)  # only now, after every authorization step
            try:
                result = await self.app.state.mcp_client.call_tool(mcp_target, args, token)
            except McpIdentityMismatch as exc:
                await self._quarantine(server_id)
                raise Denied(
                    "server_identity_mismatch", "mcp_server_identity_mismatch",
                    f"server_id={server_id} reported={exc.reported}",
                ) from exc  # fmt: skip
            except McpError as exc:
                if exc.transient or exc.reason == "server_error":
                    await breaker.record_failure()
                security = (
                    "mcp_destination_blocked" if exc.reason in ("destination_blocked", "redirect_blocked") else None
                )
                raise Denied(
                    exc.reason, security, f"server_id={server_id}", status="failed", transient=exc.transient
                ) from exc
            await breaker.record_success()
        finally:
            await release_slot(self.redis, slot_key, member)
        out: dict[str, Any] = {"content": result.content, "structured": result.structured, "is_error": result.is_error}
        scrubbed, echoed = _scrub(out, token or "")
        if echoed:
            trace.security.append(("credential_echoed", f"server_id={server_id}"))
        return scrubbed

    async def _credential(self, target: dict[str, Any]) -> str | None:
        ref = target.get("vault_ref")
        if not ref:
            return None
        vault = getattr(self.app.state, "vault", None)
        if vault is None:
            raise Denied("credential_unavailable", status="failed")
        try:
            values = await vault.read(str(ref))
        except VaultError as exc:
            raise Denied("credential_unavailable", status="failed") from exc
        secret = values.get("token")
        if secret is None:
            raise Denied("credential_unavailable", status="failed")
        return str(secret.get_secret_value())

    async def _quarantined(self, server_id: uuid.UUID) -> bool:
        local: set[str] = self.app.state.__dict__.setdefault("mcp_quarantine", set())
        if str(server_id) in local:
            return True
        if self.redis is None:
            return False
        return bool(await self.redis.get(f"te:mcp:quarantine:{server_id}"))

    async def _quarantine(self, server_id: uuid.UUID) -> None:
        self.app.state.__dict__.setdefault("mcp_quarantine", set()).add(str(server_id))
        if self.redis is not None:
            await self.redis.set(f"te:mcp:quarantine:{server_id}", "1", ex=self.settings.mcp_quarantine_seconds)

    # ------------------------------------------------------------------ recording
    def _denied(self, d: Denied, trace: Trace) -> Outcome:
        if d.security:
            trace.security.append((d.security, d.detail))
        return Outcome(d.status, reason=d.reason, transient=d.transient)

    async def _record(
        self,
        call: ToolCall,
        trace: Trace,
        outcome: Outcome,
        started: float,
        finalize: Callable[[AsyncSession, Outcome], Awaitable[None]] | None,
    ) -> None:
        spec = trace.spec or {}
        duration = int((time.monotonic() - started) * 1000)
        ids = {k: _uuid(call.context.get(k)) for k in ("call_id", "contact_id", "thread_id")}
        ids.update({k: _uuid(v) for k, v in trace.bound.items()})
        tool_id = call.tool_id if TOOL_ID_RE.match(call.tool_id) and len(call.tool_id) <= 80 else "invalid"
        async with self.app.state.db.tenant_session(call.tenant_id, call.actor) as s:
            await s.execute(
                tool_execution_records.insert().values(
                    id=trace.record_id,
                    tenant_id=call.tenant_id,
                    tool_id=tool_id,
                    tool_version=spec.get("version"),
                    risk_class=spec.get("risk_class"),
                    target_kind=(spec.get("target") or {}).get("kind"),
                    mcp_server_id=trace.server_id,
                    agent_id=call.agent_id,
                    actor_type=call.actor.type,
                    actor_id=call.actor.id[:128],
                    caller=call.caller[:128],
                    args_hash=trace.args_hash,
                    idempotency_key=call.idempotency_key[:255],
                    status=outcome.status,
                    reason=(outcome.reason or None) and outcome.reason[:64],
                    approval_id=outcome.approval_id or trace.approval_id,
                    policy_version=spec.get("policy_version"),
                    duration_ms=duration,
                    output_bytes=trace.output_bytes,
                    correlation_id=correlation_id_var.get(),
                    request_id=request_id_var.get(),
                    **ids,
                )
            )
            action, result = STATUS_AUDIT[outcome.status]
            await audit(
                s, call.actor, action, "tool_execution", trace.record_id, tenant_id=call.tenant_id, outcome=result,
                tool_id=tool_id, tool_version=spec.get("version"), risk_class=spec.get("risk_class"),
                agent_id=str(call.agent_id), reason=outcome.reason, policy_version=spec.get("policy_version"),
                approval_id=str(outcome.approval_id) if outcome.approval_id else None, duration_ms=duration,
                mcp_server_id=str(trace.server_id) if trace.server_id else None, caller=call.caller,
            )  # fmt: skip
            if trace.sensitive_read and outcome.status == "executed":
                await audit(
                    s, call.actor, "ai.tool.sensitive_read", "tool_execution", trace.record_id,
                    tenant_id=call.tenant_id, tool_id=tool_id, agent_id=str(call.agent_id),
                    contact_id=trace.bound.get("contact_id"),
                )  # fmt: skip
            for kind, detail in trace.security:
                await security_event(
                    s, call.actor, call.tenant_id, kind, agent_id=call.agent_id, tool_id=tool_id, detail=detail
                )
            if finalize is not None:
                await finalize(s, outcome)
        log.info(
            "tool_call",
            extra={
                "tool_id": tool_id, "status": outcome.status, "reason": outcome.reason, "agent_id": str(call.agent_id),
                "record_id": str(trace.record_id), "duration_ms": duration,
            },
        )  # fmt: skip


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None
