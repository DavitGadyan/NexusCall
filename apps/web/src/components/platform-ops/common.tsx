"use client";
/**
 * Building blocks shared by the integrations, analytics, tenant AI and platform-operations pages. Sub-navs
 * and the mutation error display come from the CRM module helpers so every module behaves the same.
 */
import { ShieldOff } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { Badge, ErrorState, LoadingState } from "@/components/ui";
import { SubNav } from "@/components/crm/common";
import { useCan } from "@/components/shell/session-context";
import type { ApiError } from "@/lib/client/api";

export { MutationError, StepUpHint } from "@/components/crm/common";

type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";

/** Hides a whole page without its read permission (UI only; the API answers 403 anyway). */
export function PermissionGate({
  permission,
  what,
  back,
  children,
}: {
  permission: string;
  what: string;
  back: { href: string; label: string };
  children: React.ReactNode;
}) {
  const can = useCan();
  if (can(permission)) return <>{children}</>;
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-16 text-center" data-testid="module-denied">
      <ShieldOff className="h-8 w-8 text-warn" aria-hidden />
      <h1 className="text-lg font-semibold">You don&apos;t have access to {what}</h1>
      <p className="text-sm text-muted">Your roles do not include this area. Ask an administrator if you need access.</p>
      <Link href={back.href} className="mt-2 text-sm font-medium text-brand hover:underline">
        {back.label}
      </Link>
    </div>
  );
}

export const WORKSPACE_BACK = { href: "/app", label: "Back to the workspace" };
export const ADMIN_BACK = { href: "/admin", label: "Back to workspace admin" };
export const PLATFORM_BACK = { href: "/platform", label: "Back to the platform overview" };

export function AiPlatformNav() {
  return (
    <SubNav
      label="AI gateway sections"
      items={[
        { href: "/platform/ai/models", label: "Models & routing", permission: "platform:ai:read" },
        { href: "/platform/ai/allowlists", label: "Tenant allowlists", permission: "platform:ai:read" },
        { href: "/platform/ai/kill-switches", label: "Kill switches", permission: "platform:ai:read" },
        { href: "/platform/ai/tools", label: "MCP & tools", permission: "platform:ai:read" },
        { href: "/platform/ai/policies", label: "Policies", permission: "platform:ai:read" },
        { href: "/platform/ai/security", label: "Security events", permission: "platform:ai:read" },
      ]}
    />
  );
}

export function InfraNav() {
  return (
    <SubNav
      label="Infrastructure sections"
      items={[
        { href: "/platform/infra/services", label: "Services", permission: "platform:infra:read" },
        { href: "/platform/infra/queues", label: "Queues & dead letters", permission: "platform:infra:read" },
      ]}
    />
  );
}

export function ToneBadge({ tone, label, title }: { tone: Tone; label: string; title?: string }) {
  return (
    <span title={title}>
      <Badge tone={tone}>{label}</Badge>
    </span>
  );
}

/** Loading / error wrapper for a card body backed by one `useApi` resource. */
export function Resource<T>({
  state,
  label,
  children,
}: {
  state: { data: T | undefined; error: ApiError | undefined; loading: boolean; reload: () => void };
  label: string;
  children: (data: T) => React.ReactNode;
}) {
  if (state.loading && state.data === undefined) return <LoadingState label={`Loading ${label}`} />;
  if (state.error && state.data === undefined) return <ErrorState problem={state.error.problem} onRetry={state.reload} />;
  if (state.data === undefined) return null;
  return <>{children(state.data)}</>;
}

export function Mono({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <span className="break-all font-mono text-xs" title={title}>
      {children}
    </span>
  );
}
