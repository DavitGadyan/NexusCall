/** Information architecture (build.md §21). Each item declares the permission that reveals it. */
export type NavItem = { label: string; href: string; permission?: string; children?: NavItem[] };
export type NavSection = { label: string; items: NavItem[] };

export const workspaceNav: NavSection[] = [
  { label: "Operate", items: [
    { label: "Dashboard", href: "/app", permission: "analytics:read" },
    { label: "Leads", href: "/app/crm/leads", permission: "leads:read" },
    { label: "Contacts", href: "/app/crm/contacts", permission: "leads:read" },
    { label: "Imports", href: "/app/crm/imports", permission: "leads:create" },
    { label: "Call campaigns", href: "/app/campaigns/calls", permission: "campaigns:read" },
    { label: "Email campaigns", href: "/app/campaigns/email", permission: "emails:campaigns:manage" },
  ]},
  { label: "Conversations", items: [
    { label: "Live calls", href: "/app/calls/live", permission: "calls:read" },
    { label: "Call history", href: "/app/calls", permission: "calls:read" },
    { label: "Inbox", href: "/app/inbox", permission: "emails:inbox:read" },
    { label: "Approval queue", href: "/app/inbox/drafts", permission: "emails:drafts:review" },
    { label: "Bookings", href: "/app/bookings", permission: "bookings:read" },
    { label: "AI approvals", href: "/app/approvals", permission: "ai:approvals:decide" },
  ]},
  { label: "Configure", items: [
    { label: "AI agents", href: "/app/agents", permission: "ai:agents:read" },
    { label: "Knowledge", href: "/app/knowledge", permission: "ai:agents:read" },
    { label: "Analytics", href: "/app/analytics", permission: "analytics:read" },
    { label: "Integrations", href: "/app/integrations", permission: "integrations:read" },
  ]},
];

export const tenantAdminNav: NavSection[] = [
  { label: "Workspace admin", items: [
    { label: "Team & access", href: "/admin/team", permission: "tenant:users:manage" },
    { label: "Invitations", href: "/admin/invitations", permission: "tenant:users:manage" },
    { label: "Settings", href: "/admin/settings", permission: "tenant:manage" },
    { label: "Communication policy", href: "/admin/policies", permission: "tenant:manage" },
    { label: "Email senders", href: "/admin/email", permission: "emails:campaigns:manage" },
    { label: "Phone numbers", href: "/admin/numbers", permission: "calls:manage" },
    { label: "AI settings", href: "/admin/ai", permission: "tenant:manage" },
    { label: "Provider connections", href: "/admin/connections", permission: "integrations:manage" },
    { label: "Support access", href: "/admin/support-access", permission: "audit:read" },
    { label: "Audit log", href: "/admin/audit", permission: "audit:read" },
  ]},
];

export const platformNav: NavSection[] = [
  { label: "Platform", items: [
    { label: "Overview", href: "/platform", permission: "platform:overview:read" },
    { label: "Tenants", href: "/platform/tenants", permission: "platform:tenants:read" },
  ]},
  { label: "Identity & access", items: [
    { label: "Users", href: "/platform/users", permission: "platform:users:read" },
    { label: "Service accounts", href: "/platform/service-accounts", permission: "platform:iam:manage" },
    { label: "Roles & permissions", href: "/platform/roles", permission: "platform:iam:read" },
    { label: "Privileged requests", href: "/platform/privileged-requests", permission: "platform:iam:read" },
    { label: "Auth events & IdP", href: "/platform/auth-events", permission: "platform:iam:read" },
  ]},
  { label: "Security", items: [
    { label: "RLS coverage", href: "/platform/security/rls", permission: "platform:security:read" },
    { label: "Secrets health", href: "/platform/security/secrets", permission: "platform:secrets:read" },
    { label: "Break-glass", href: "/platform/security/break-glass", permission: "platform:support:request" },
    { label: "Audit", href: "/platform/audit", permission: "platform:audit:read" },
  ]},
  { label: "AI gateway", items: [
    { label: "Models & routing", href: "/platform/ai/models", permission: "platform:ai:read" },
    { label: "MCP & tools", href: "/platform/ai/tools", permission: "platform:ai:read" },
    { label: "Policies", href: "/platform/ai/policies", permission: "platform:ai:read" },
    { label: "Security events", href: "/platform/ai/security", permission: "platform:ai:read" },
  ]},
  { label: "Infrastructure", items: [
    { label: "Services", href: "/platform/infra/services", permission: "platform:infra:read" },
    { label: "Queues & DLQ", href: "/platform/infra/queues", permission: "platform:infra:read" },
    { label: "Integrations", href: "/platform/integrations", permission: "platform:integrations:read" },
    { label: "Usage", href: "/platform/usage", permission: "platform:usage:read" },
    { label: "Settings", href: "/platform/settings", permission: "platform:settings:read" },
    { label: "Feature flags", href: "/platform/settings?tab=flags", permission: "platform:settings:read" },
    { label: "Kill switches", href: "/platform/settings?tab=kill_switches", permission: "platform:settings:read" },
  ]},
];

export function filterNav(sections: NavSection[], permissions: ReadonlySet<string>,
                          can: (p: ReadonlySet<string>, r: string) => boolean): NavSection[] {
  return sections
    .map((s) => ({ ...s, items: s.items.filter((i) => !i.permission || can(permissions, i.permission)) }))
    .filter((s) => s.items.length > 0);
}
