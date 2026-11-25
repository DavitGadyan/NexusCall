/** Loading, error, conflict and locked states shared by every page. */
import { AlertTriangle, Loader2, Lock, RefreshCw } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";
import type { Problem } from "@/lib/problem";

export function Spinner({ className, label = "Loading" }: { className?: string; label?: string }) {
  return (
    <span role="status" className="inline-flex items-center">
      <Loader2 className={cn("h-4 w-4 animate-spin text-muted", className)} aria-hidden />
      <span className="sr-only">{label}</span>
    </span>
  );
}

export function LoadingState({ label = "Loading…", rows = 4 }: { label?: string; rows?: number }) {
  return (
    <div aria-busy="true" aria-live="polite" className="px-5 py-6">
      <p className="sr-only">{label}</p>
      <div className="flex flex-col gap-3">
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="h-4 animate-pulse rounded bg-surface-2" style={{ width: `${90 - i * 12}%` }} />
        ))}
      </div>
    </div>
  );
}

export function ErrorState({
  problem,
  title,
  onRetry,
}: {
  problem?: Pick<Problem, "detail" | "status" | "request_id"> | null;
  title?: string;
  onRetry?: () => void;
}) {
  const forbidden = problem?.status === 403;
  return (
    <div role="alert" className="flex flex-col items-center gap-2 px-6 py-10 text-center">
      <AlertTriangle className={cn("h-5 w-5", forbidden ? "text-warn" : "text-danger")} aria-hidden />
      <p className="text-sm font-medium">{title ?? (forbidden ? "You don't have access to this" : "Something went wrong")}</p>
      {problem?.detail ? <p className="max-w-md text-sm text-muted">{problem.detail}</p> : null}
      {problem?.request_id ? <p className="font-mono text-xs text-muted">Request ID {problem.request_id}</p> : null}
      {onRetry && !forbidden ? (
        <button type="button" onClick={onRetry} className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-2">
          <RefreshCw className="h-3.5 w-3.5" aria-hidden /> Try again
        </button>
      ) : null}
    </div>
  );
}

export function Notice({
  tone = "info",
  title,
  children,
  action,
}: {
  tone?: "info" | "warn" | "danger" | "ok";
  title?: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div
      role={tone === "danger" ? "alert" : "note"}
      className={cn(
        "flex flex-wrap items-start justify-between gap-3 rounded-md border px-4 py-3 text-sm",
        tone === "info" && "border-brand/30 bg-brand/5",
        tone === "warn" && "border-warn/40 bg-warn/10",
        tone === "danger" && "border-danger/40 bg-danger/10",
        tone === "ok" && "border-ok/40 bg-ok/10",
      )}
    >
      <div className="min-w-0">
        {title ? <p className="font-medium">{title}</p> : null}
        {children ? <div className={cn(title && "mt-0.5", "text-fg/80")}>{children}</div> : null}
      </div>
      {action}
    </div>
  );
}

/** Optimistic-lock conflict (409): offer a reload instead of overwriting someone else's change. */
export function ConflictNotice({ onReload, children }: { onReload: () => void; children?: React.ReactNode }) {
  return (
    <Notice
      tone="warn"
      title="Changed by someone else"
      action={
        <button type="button" onClick={onReload} className="rounded-md border border-border bg-surface px-3 py-1.5 text-sm hover:bg-surface-2">
          Reload latest
        </button>
      }
    >
      {children ?? "Your copy is out of date. Reload to see the latest version, then reapply your change."}
    </Notice>
  );
}

export function LockedBadge({ reason }: { reason: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-surface-2 px-2 py-0.5 text-xs text-muted" title={reason}>
      <Lock className="h-3 w-3" aria-hidden />
      <span>Locked</span>
      <span className="sr-only">: {reason}</span>
    </span>
  );
}
