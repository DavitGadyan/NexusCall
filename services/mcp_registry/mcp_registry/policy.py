"""Platform tool policy (build.md §24.8 "policy editing and deployment") and effective-grant computation.

A policy can only TIGHTEN the code-defined invariants: class 4 is never agent-accessible, class 3 always
needs a human approval and MCP servers must be pinned. Validation rejects anything weaker, so a proposal
cannot loosen the security floor even with two approvers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TOOL_ID_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_agent_risk_class: int = Field(3, ge=0, le=3)
    approval_required_classes: list[int] = Field(default_factory=lambda: [3], max_length=4)
    disabled_tools: list[str] = Field(default_factory=list, max_length=200)
    agent_calls_per_minute: int = Field(60, ge=1, le=6000)
    mcp_tools_enabled: bool = True
    mcp_require_pinned: Literal[True] = True

    @field_validator("approval_required_classes")
    @classmethod
    def _classes(cls, v: list[int]) -> list[int]:
        values = sorted(set(v))
        if any(c not in (0, 1, 2, 3) for c in values):
            raise ValueError("approval classes must be within 0..3")
        if 3 not in values:
            raise ValueError("class 3 tools always require approval")
        return values

    @field_validator("disabled_tools")
    @classmethod
    def _tools(cls, v: list[str]) -> list[str]:
        import re

        for t in v:
            if not re.match(TOOL_ID_PATTERN, t) or len(t) > 80:
                raise ValueError("invalid tool id in disabled_tools")
        return sorted(set(v))


DEFAULT_POLICY = ToolPolicy()


@dataclass(frozen=True, slots=True)
class Effective:
    enabled: bool
    granted: bool
    requires_approval: bool
    rate_per_minute: int
    reason: str | None


def effective(
    tool: dict[str, Any],
    server: dict[str, Any] | None,
    policy: ToolPolicy,
    setting: dict[str, Any] | None,
    grant: dict[str, Any] | None,
) -> Effective:
    """Combine platform definition, MCP server state, platform policy, tenant overlay and agent grant.

    Every layer can only remove capability or add approval; none can add capability."""
    risk = int(tool["risk_class"])
    reason: str | None = None
    enabled = bool(tool["enabled"]) and not tool["revoked"]
    if not enabled:
        reason = "tool_revoked" if tool["revoked"] else "tool_disabled"
    if enabled and tool["kind"] == "mcp":
        if server is None or not server["enabled"] or server["revoked"]:
            enabled, reason = False, "server_disabled"
        elif not (server["pinned_version"] and server["pinned_sha256"] and server["reviewed_by"]):
            enabled, reason = False, "server_not_pinned"
        elif not policy.mcp_tools_enabled:
            enabled, reason = False, "policy_disabled"
    if enabled and (tool["tool_id"] in policy.disabled_tools or policy.max_agent_risk_class < risk < 4):
        enabled, reason = False, "policy_disabled"
    if enabled and setting is not None and not setting["enabled"]:
        enabled, reason = False, "tenant_disabled"
    granted = enabled and grant is not None and risk <= 3
    if enabled and not granted:
        reason = "privileged_tool" if risk >= 4 else "not_granted"
    requires_approval = (
        bool(tool["requires_approval"])
        or risk >= 3
        or risk in policy.approval_required_classes
        or bool(setting and setting["requires_approval"])
        or bool(grant and grant["requires_approval"])
    )
    rates = [int(tool["rate_per_minute"])]
    if setting and setting.get("rate_per_minute"):
        rates.append(int(setting["rate_per_minute"]))
    if grant and grant.get("calls_per_minute"):
        rates.append(int(grant["calls_per_minute"]))
    return Effective(enabled, granted, requires_approval, min(rates), reason)
