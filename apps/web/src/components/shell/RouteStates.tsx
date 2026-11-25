"use client";
/** Shared `loading.tsx` / `error.tsx` bodies for the surfaces. */
import { ErrorState, LoadingState } from "@/components/ui/states";

export function RouteLoading() {
  return (
    <div className="rounded-lg border border-border bg-surface">
      <LoadingState label="Loading page" rows={6} />
    </div>
  );
}

export function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="rounded-lg border border-border bg-surface">
      <ErrorState
        title="This page failed to load"
        problem={{ status: 500, detail: "An unexpected error occurred while rendering.", request_id: error.digest ?? null }}
        onRetry={reset}
      />
    </div>
  );
}
