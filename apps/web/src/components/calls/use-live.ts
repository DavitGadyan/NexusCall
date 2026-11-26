"use client";
/**
 * Live-refresh hooks over the shared `useLiveEvents` stream. Events are refresh hints only: the page always
 * re-fetches through the API. When the stream is not open the hooks fall back to polling.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { LiveEvent } from "@/lib/live-events";
import { type LiveStatus, useLiveEvents } from "@/lib/client/use-live-events";

type Opts = {
  events: readonly string[];
  /** only react to events that concern this view (e.g. one call id) */
  match?: (e: LiveEvent) => boolean;
  /** polling interval while the stream is down */
  pollMs?: number;
  /** slow safety poll while the stream is up (0 disables) */
  slowPollMs?: number;
  enabled?: boolean;
};

/** Re-runs `reload` (coalesced) on matching events, and polls when the stream is unavailable. */
export function useLiveRefresh(reload: () => void, opts: Opts): LiveStatus {
  const { events, match, pollMs = 10_000, slowPollMs = 0, enabled = true } = opts;
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  const matchRef = useRef(match);
  matchRef.current = match;
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);

  const trigger = useCallback(() => {
    if (pending.current) return;
    pending.current = setTimeout(() => {
      pending.current = null;
      reloadRef.current();
    }, 300);
  }, []);

  useEffect(
    () => () => {
      if (pending.current) clearTimeout(pending.current);
    },
    [],
  );

  const status = useLiveEvents(
    (e) => {
      if (!matchRef.current || matchRef.current(e)) trigger();
    },
    { events, enabled },
  );

  useEffect(() => {
    if (!enabled) return;
    const ms = status === "open" ? slowPollMs : pollMs;
    if (!ms) return;
    const t = setInterval(() => {
      if (typeof document === "undefined" || document.visibilityState === "visible") reloadRef.current();
    }, ms);
    return () => clearInterval(t);
  }, [status, enabled, pollMs, slowPollMs]);

  return status;
}

/**
 * For paginated lists: instead of reloading under the user's cursor, flag that newer data exists so the page
 * can offer "Refresh".
 */
export function useLiveHint(opts: Pick<Opts, "events" | "match" | "enabled">): { stale: boolean; clear: () => void; status: LiveStatus } {
  const [stale, setStale] = useState(false);
  const matchRef = useRef(opts.match);
  matchRef.current = opts.match;
  const status = useLiveEvents(
    (e) => {
      if (!matchRef.current || matchRef.current(e)) setStale(true);
    },
    { events: opts.events, enabled: opts.enabled },
  );
  const clear = useCallback(() => setStale(false), []);
  return { stale, clear, status };
}

/** A clock that ticks every `ms` (for durations and countdowns). */
export function useNow(ms = 1000, enabled = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms, enabled]);
  return now;
}

/** A per-dialog Idempotency-Key: stable across retries of the same action, new each time the dialog opens. */
export function useIdempotencyKey(open: boolean): string {
  const ref = useRef<{ open: boolean; key: string }>({ open: false, key: "" });
  if (open && !ref.current.open) ref.current = { open: true, key: crypto.randomUUID() };
  else if (!open && ref.current.open) ref.current = { open: false, key: ref.current.key };
  return ref.current.key;
}
