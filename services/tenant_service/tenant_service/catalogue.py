"""Permission catalogue and system roles (build.md §5.2, §1.1).

The catalogue is code-defined and seeded by migration; custom roles may only be
composed from catalogue permissions (no free-form permission strings).
"""

from __future__ import annotations

from dataclasses import dataclass

TENANT_PERMISSIONS: dict[str, str] = {
    "tenant:manage": "Manage tenant profile, branding and settings",
    "tenant:users:manage": "Invite users, manage memberships and roles",
    "leads:read": "View leads and contacts",
    "leads:create": "Create and import leads",
    "leads:update": "Edit leads, consent and suppression",
    "leads:export": "Export lead data (audited)",
    "campaigns:read": "View campaigns",
    "campaigns:create": "Create campaigns",
    "campaigns:manage": "Edit, pause and cancel campaigns; configure AI agents",
    "campaigns:launch": "Launch campaigns (audited)",
    "calls:read": "View calls, live activity and outcomes",
    "calls:manage": "Manage calls, phone numbers and handoffs",
    "calls:recordings:read": "Access recordings and transcripts (audited)",
    "bookings:read": "View bookings and availability",
    "bookings:manage": "Create, reschedule and cancel bookings",
    "emails:campaigns:manage": "Manage email campaigns, templates and senders",
    "emails:send": "Send transactional email",
    "emails:inbox:read": "Read inbox threads",
    "emails:drafts:review": "Review, edit and reject AI drafts",
    "emails:drafts:approve": "Approve AI drafts for sending (audited)",
    "integrations:read": "View provider connections, webhooks and delivery history",
    "integrations:manage": "Manage provider connections and webhooks",
    "ai:agents:read": "View AI agents, prompts and knowledge sources",
    "ai:agents:manage": "Configure AI agents, prompts, knowledge sources and tool grants",
    "ai:approvals:decide": "Approve or reject AI-proposed customer-facing actions (audited)",
    "ai:usage:read": "View AI usage, cost and activity",
    "analytics:read": "View dashboards and reports",
    "analytics:export": "Export analytics (audited)",
    "audit:read": "View the tenant audit log",
}

PLATFORM_PERMISSIONS: dict[str, str] = {
    "platform:overview:read": "View the platform overview",
    "platform:tenants:read": "View tenants",
    "platform:tenants:manage": "Create, suspend, reactivate and archive tenants",
    "platform:users:read": "View users and memberships across tenants",
    "platform:users:manage": "Deactivate users, revoke sessions, manage memberships",
    "platform:iam:read": "View roles, permissions, IdP status and auth events",
    "platform:iam:manage": "Manage roles, service accounts and platform admins",
    "platform:security:read": "View RLS coverage and security posture",
    "platform:secrets:read": "View secret reference metadata (never values)",
    "platform:secrets:manage": "Rotate or revoke secrets",
    "platform:support:request": "Request time-limited support access to a tenant",
    "platform:support:approve": "Approve support access requests",
    "platform:audit:read": "Search platform-wide audit events",
    "platform:ai:read": "View AI gateway configuration and events",
    "platform:ai:manage": "Manage AI providers, models, tools and kill switches",
    "platform:infra:read": "View services, queues and workers",
    "platform:queues:replay": "Replay or discard dead-lettered jobs",
    "platform:integrations:read": "View platform provider connections",
    "platform:integrations:manage": "Manage platform provider connections",
    "platform:usage:read": "View usage and cost",
    "platform:settings:read": "View platform settings",
    "platform:settings:manage": "Change platform settings and feature flags",
}

ALL_TENANT = frozenset(TENANT_PERMISSIONS)
ALL_PLATFORM = frozenset(PLATFORM_PERMISSIONS)


@dataclass(frozen=True, slots=True)
class SystemRole:
    key: str
    name: str
    scope: str  # tenant | platform
    permissions: frozenset[str]
    description: str
    requires_mfa: bool = False


SYSTEM_ROLES: tuple[SystemRole, ...] = (
    SystemRole("tenant_owner", "Tenant owner", "tenant", ALL_TENANT, "Owns the workspace", True),
    SystemRole(
        "tenant_admin",
        "Tenant admin",
        "tenant",
        ALL_TENANT - {"leads:export", "analytics:export"},
        "Manages users, settings and integrations",
        True,
    ),
    SystemRole(
        "campaign_manager",
        "Campaign manager",
        "tenant",
        frozenset(
            {
                "bookings:read",
                "ai:agents:read",
                "ai:agents:manage",
                "ai:usage:read",
                "leads:read",
                "leads:create",
                "leads:update",
                "campaigns:read",
                "campaigns:create",
                "campaigns:manage",
                "campaigns:launch",
                "calls:read",
                "bookings:manage",
                "analytics:read",
            }
        ),
        "Creates and operates call campaigns",
    ),
    SystemRole(
        "call_supervisor",
        "Call supervisor",
        "tenant",
        frozenset(
            {
                "bookings:read",
                "ai:agents:read",
                "ai:approvals:decide",
                "leads:read",
                "campaigns:read",
                "calls:read",
                "calls:manage",
                "calls:recordings:read",
                "bookings:manage",
                "analytics:read",
            }
        ),
        "Reviews calls, live activity and recordings",
    ),
    SystemRole(
        "call_agent",
        "Call agent",
        "tenant",
        frozenset({"leads:read", "calls:read", "bookings:read", "bookings:manage"}),
        "Handles handoffs and assigned leads",
    ),
    SystemRole(
        "email_manager",
        "Email manager",
        "tenant",
        frozenset(
            {
                "ai:agents:read",
                "ai:approvals:decide",
                "integrations:read",
                "leads:read",
                "campaigns:read",
                "emails:campaigns:manage",
                "emails:send",
                "emails:inbox:read",
                "emails:drafts:review",
                "emails:drafts:approve",
                "analytics:read",
            }
        ),
        "Runs email campaigns and approves AI drafts",
        True,
    ),
    SystemRole(
        "analyst",
        "Analyst (read-only)",
        "tenant",
        frozenset(
            {
                "bookings:read",
                "ai:usage:read",
                "leads:read",
                "campaigns:read",
                "calls:read",
                "analytics:read",
            }
        ),
        "Views dashboards and reports",
    ),
    SystemRole("platform_owner", "Platform owner", "platform", ALL_PLATFORM, "Operates the agency platform", True),
    SystemRole(
        "platform_operator",
        "Platform operator",
        "platform",
        frozenset({p for p in ALL_PLATFORM if p.endswith(":read")} | {"platform:support:request"}),
        "Day-to-day platform operations (read + support requests)",
        True,
    ),
)

ROLE_BY_KEY = {r.key: r for r in SYSTEM_ROLES}

# Read-only scopes a time-limited support grant may confer (never write, never export).
SUPPORT_SCOPES: dict[str, frozenset[str]] = {
    "read_config": frozenset({"audit:read", "analytics:read"}),
    "read_operations": frozenset({"campaigns:read", "calls:read", "analytics:read", "audit:read"}),
    "read_crm": frozenset({"leads:read", "audit:read"}),
}
