"""Typed, validated settings sections (build.md §18.2).

Tenant sections are validated against the *current platform bounds* so tenants
cannot override platform security controls. Some controls are locked by design
(e.g. AI draft auto-send stays off; MFA for admins stays on).
"""

from __future__ import annotations

import re
from typing import Any, Literal
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_TZ = frozenset(available_timezones())
MODULES = ("crm", "calls", "email_campaigns", "inbox", "bookings", "analytics", "integrations", "ai_agents")


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Window(_S):
    start: str = "09:00"
    end: str = "20:00"
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = ["mon", "tue", "wed", "thu", "fri"]

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("expected HH:MM")
        return v

    @model_validator(mode="after")
    def _order(self) -> Window:
        if self.start >= self.end:
            raise ValueError("window start must be before end")
        return self


def _https_url(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    if not v.startswith("https://") or len(v) > 500:
        raise ValueError("must be an https:// URL")
    return v


# ---------------- platform ----------------
class PlatformBranding(_S):
    platform_name: str = Field("NexusCall", min_length=2, max_length=80)
    primary_color: str = "#4F46E5"
    logo_url: str | None = None
    default_theme: Literal["system", "light", "dark"] = "system"
    default_landing_page: Literal["/app", "/app/calls", "/app/inbox", "/app/analytics"] = "/app"

    @field_validator("primary_color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not _COLOR.match(v):
            raise ValueError("expected #RRGGBB")
        return v

    @field_validator("logo_url")
    @classmethod
    def _logo(cls, v: str | None) -> str | None:
        return _https_url(v)


class PlatformSecurity(_S):
    session_idle_minutes: int = Field(30, ge=5, le=240)
    session_max_hours: int = Field(10, ge=1, le=24)
    step_up_max_age_seconds: int = Field(300, ge=60, le=900)
    mfa_required_for_admins: Literal[True] = True
    invitation_ttl_hours: int = Field(168, ge=1, le=336)
    support_grant_max_hours: int = Field(4, ge=1, le=24)


class PlatformLimits(_S):
    max_concurrent_calls_per_tenant: int = Field(50, ge=1, le=1000)
    max_daily_calls_per_tenant: int = Field(20000, ge=1, le=1_000_000)
    max_email_daily_quota: int = Field(100000, ge=1, le=10_000_000)
    api_rate_limit_per_minute: int = Field(600, ge=10, le=100000)
    campaign_launches_per_hour: int = Field(20, ge=1, le=1000)
    max_ai_monthly_budget_usd: int = Field(5000, ge=0, le=1_000_000)


class PlatformRetention(_S):
    max_recordings_days: int = Field(730, ge=1, le=3650)
    default_recordings_days: int = Field(90, ge=1, le=3650)
    max_transcripts_days: int = Field(730, ge=1, le=3650)
    max_email_content_days: int = Field(730, ge=1, le=3650)
    max_attachments_days: int = Field(365, ge=1, le=3650)
    audit_retention_days: int = Field(2555, ge=365, le=3650)


class PlatformMaintenance(_S):
    mode: Literal["off", "read_only", "maintenance"] = "off"
    message: str = Field("", max_length=300)


class PlatformModules(_S):
    enabled: list[Literal[MODULES]] = list(MODULES)  # type: ignore[valid-type]


class PlatformKillSwitches(_S):
    calls_enabled: bool = True
    emails_enabled: bool = True
    ai_enabled: bool = True
    reason: str = Field("", max_length=300)


PLATFORM_SECTIONS: dict[str, type[_S]] = {
    "branding": PlatformBranding,
    "security": PlatformSecurity,
    "limits": PlatformLimits,
    "retention": PlatformRetention,
    "maintenance": PlatformMaintenance,
    "modules": PlatformModules,
    "kill_switches": PlatformKillSwitches,
}
HIGH_RISK_PLATFORM_SECTIONS = frozenset({"security", "kill_switches", "maintenance", "limits"})


# ---------------- tenant ----------------
class TenantProfile(_S):
    name: str = Field(..., min_length=2, max_length=200)
    timezone: str = "UTC"
    locale: str = Field("en-US", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    business_hours: Window = Window()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if v not in _TZ:
            raise ValueError("unknown IANA timezone")
        return v


class TenantBranding(_S):
    primary_color: str = "#4F46E5"
    logo_url: str | None = None

    @field_validator("primary_color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not _COLOR.match(v):
            raise ValueError("expected #RRGGBB")
        return v

    @field_validator("logo_url")
    @classmethod
    def _logo(cls, v: str | None) -> str | None:
        return _https_url(v)


class TenantCommunication(_S):
    calls_enabled: bool = True  # tenant kill switch
    emails_enabled: bool = True  # tenant kill switch
    calling_window: Window = Window(start="09:00", end="20:00")
    email_window: Window = Window(start="08:00", end="20:00", days=["mon", "tue", "wed", "thu", "fri", "sat"])
    max_concurrent_calls: int = Field(5, ge=1)
    max_daily_calls: int = Field(1000, ge=1)
    email_daily_quota: int = Field(5000, ge=1)
    allowed_call_countries: list[str] = Field(default_factory=lambda: ["US"])
    ai_disclosure_enabled: bool = True
    recording_enabled: bool = True
    recording_consent_prompt: bool = True
    honor_opt_out_across_channels: Literal[True] = True

    @field_validator("allowed_call_countries")
    @classmethod
    def _cc(cls, v: list[str]) -> list[str]:
        if any(not re.match(r"^[A-Z]{2}$", c) for c in v) or len(v) > 50:
            raise ValueError("ISO 3166-1 alpha-2 country codes")
        return sorted(set(v))


class TenantRetention(_S):
    recordings_days: int = Field(90, ge=1)
    transcripts_days: int = Field(180, ge=1)
    email_content_days: int = Field(365, ge=1)
    attachments_days: int = Field(180, ge=1)


class TenantAi(_S):
    allowed_models: list[str] = Field(default_factory=lambda: ["claude-haiku-5-5", "claude-sonnet-5-5"])
    monthly_budget_usd: int = Field(200, ge=0)
    draft_auto_send: Literal[False] = False  # build.md §3.1L default; no auto-send policy in this release
    data_classification_max: Literal["internal", "confidential", "restricted"] = "confidential"
    store_prompt_content: Literal[False] = False


class TenantApproval(_S):
    require_approval_for_ai_email: Literal[True] = True
    approver_permission: Literal["emails:drafts:approve"] = "emails:drafts:approve"
    escalate_sensitive_intents: bool = True


class TenantModules(_S):
    enabled: list[Literal[MODULES]] = list(MODULES)  # type: ignore[valid-type]


TENANT_SECTIONS: dict[str, type[_S]] = {
    "profile": TenantProfile,
    "branding": TenantBranding,
    "communication": TenantCommunication,
    "retention": TenantRetention,
    "ai": TenantAi,
    "approval": TenantApproval,
    "modules": TenantModules,
}


class BoundsError(ValueError):
    pass


def enforce_platform_bounds(section: str, data: _S, platform: dict[str, dict[str, Any]]) -> None:
    """Reject tenant values above platform limits (platform policy always wins)."""

    def section_data(name: str) -> dict[str, Any]:
        return {k: v for k, v in platform.get(name, {}).items() if not k.startswith("_")}

    limits = PlatformLimits(**section_data("limits"))
    retention = PlatformRetention(**section_data("retention"))
    modules = PlatformModules(**section_data("modules"))
    problems: list[str] = []
    if isinstance(data, TenantCommunication):
        if data.max_concurrent_calls > limits.max_concurrent_calls_per_tenant:
            problems.append(f"max_concurrent_calls exceeds platform limit {limits.max_concurrent_calls_per_tenant}")
        if data.max_daily_calls > limits.max_daily_calls_per_tenant:
            problems.append(f"max_daily_calls exceeds platform limit {limits.max_daily_calls_per_tenant}")
        if data.email_daily_quota > limits.max_email_daily_quota:
            problems.append(f"email_daily_quota exceeds platform limit {limits.max_email_daily_quota}")
    if isinstance(data, TenantRetention):
        for field, cap in (
            ("recordings_days", retention.max_recordings_days),
            ("transcripts_days", retention.max_transcripts_days),
            ("email_content_days", retention.max_email_content_days),
            ("attachments_days", retention.max_attachments_days),
        ):
            if getattr(data, field) > cap:
                problems.append(f"{field} exceeds platform maximum {cap}")
    if isinstance(data, TenantAi) and data.monthly_budget_usd > limits.max_ai_monthly_budget_usd:
        problems.append(f"monthly_budget_usd exceeds platform limit {limits.max_ai_monthly_budget_usd}")
    if isinstance(data, TenantModules):
        extra = set(data.enabled) - set(modules.enabled)
        if extra:
            problems.append(f"modules not enabled by the platform: {sorted(extra)}")
    if problems:
        raise BoundsError("; ".join(problems))


def defaults_for_tenant(section: str, tenant_name: str) -> dict[str, Any]:
    model = TENANT_SECTIONS[section]
    return (model(name=tenant_name) if model is TenantProfile else model()).model_dump(mode="json")  # type: ignore[call-arg]
