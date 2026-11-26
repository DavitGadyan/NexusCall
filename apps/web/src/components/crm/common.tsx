"use client";
/** Building blocks shared by the CRM, campaign and AI module pages. */
import { ShieldOff } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";
import { cn } from "@/lib/cn";
import type { ApiError } from "@/lib/client/api";
import { Badge, ConflictNotice, FormError } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { isVersionConflict } from "./lib/http";

/** Hides a whole page when the user lacks its read permission (UI only; the API answers 403 anyway). */
export function ModuleGate({ permission, children, what }: { permission: string; children: React.ReactNode; what: string }) {
  const can = useCan();
  if (can(permission)) return <>{children}</>;
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-16 text-center" data-testid="module-denied">
      <ShieldOff className="h-8 w-8 text-warn" aria-hidden />
      <h1 className="text-lg font-semibold">You don&apos;t have access to {what}</h1>
      <p className="text-sm text-muted">Your roles in this workspace do not include this module. Ask an administrator if you need access.</p>
      <Link href="/app" className="mt-2 text-sm font-medium text-brand hover:underline">
        Back to the workspace
      </Link>
    </div>
  );
}

type SubNavItem = { href: string; label: string; permission: string };

export function SubNav({ items, label }: { items: SubNavItem[]; label: string }) {
  const can = useCan();
  const path = usePathname();
  const visible = items.filter((i) => can(i.permission));
  if (visible.length < 2) return null;
  // The most specific matching item is current ("/app/agents/evals" over "/app/agents").
  const current = visible
    .filter((i) => path === i.href || path.startsWith(`${i.href}/`))
    .sort((a, b) => b.href.length - a.href.length)[0]?.href;
  return (
    <nav aria-label={label} className="mb-4 flex gap-1 overflow-x-auto border-b border-border">
      {visible.map((i) => {
        const active = i.href === current;
        return (
          <Link
            key={i.href}
            href={i.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "-mb-px shrink-0 whitespace-nowrap border-b-2 px-3 py-2 text-sm",
              active ? "border-brand font-medium text-fg" : "border-transparent text-muted hover:text-fg",
            )}
          >
            {i.label}
          </Link>
        );
      })}
    </nav>
  );
}

export const CRM_NAV: SubNavItem[] = [
  { href: "/app/crm/leads", label: "Leads", permission: "leads:read" },
  { href: "/app/crm/contacts", label: "Contacts", permission: "leads:read" },
  { href: "/app/crm/imports", label: "Imports", permission: "leads:create" },
  { href: "/app/crm/exports", label: "Exports", permission: "leads:export" },
  { href: "/app/crm/segments", label: "Segments", permission: "campaigns:read" },
  { href: "/app/crm/suppressions", label: "Suppressions", permission: "leads:read" },
  { href: "/app/crm/fields", label: "Fields & tags", permission: "leads:read" },
];

export const AI_NAV: SubNavItem[] = [
  { href: "/app/agents", label: "Agents", permission: "ai:agents:read" },
  { href: "/app/agents/evals", label: "Evaluations", permission: "ai:agents:read" },
  { href: "/app/knowledge", label: "Knowledge", permission: "ai:agents:read" },
];

export function CrmNav() {
  return <SubNav items={CRM_NAV} label="CRM sections" />;
}

export function AiNav() {
  return <SubNav items={AI_NAV} label="AI sections" />;
}

const JOB_TONE: Record<string, "neutral" | "ok" | "warn" | "danger" | "brand"> = {
  queued: "neutral",
  processing: "brand",
  running: "brand",
  completed: "ok",
  indexed: "ok",
  failed: "danger",
  rejected: "danger",
};

export function JobBadge({ status }: { status: string }) {
  return <Badge tone={JOB_TONE[status] ?? "neutral"}>{status.replace(/_/g, " ")}</Badge>;
}

/**
 * Error display for forms: field errors are shown next to fields by the caller; an optimistic-lock 409
 * becomes a reload prompt; anything else (except step-up, which has its own dialog) is a form error.
 */
export function MutationError({ error, onReload }: { error: ApiError | null; onReload?: () => void }) {
  if (!error || error.isStepUp) return null;
  if (isVersionConflict(error) && onReload) return <ConflictNotice onReload={onReload}>{error.message}</ConflictNotice>;
  const fieldCount = Object.keys(error.fields).filter((k) => k !== "_form").length;
  if (fieldCount > 0) return <FormError message={error.fields._form ?? "Some fields need attention."} />;
  return <FormError message={error.message} />;
}

export function StepUpHint() {
  return <Badge tone="warn">Needs recent MFA</Badge>;
}
