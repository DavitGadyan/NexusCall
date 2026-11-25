"use client";
/** Header notification bell: unread count, recent items, mark read, live updates over SSE. */
import { Bell } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api } from "@/lib/client/api";
import { useLiveEvents } from "@/lib/client/use-live-events";
import { cn } from "@/lib/cn";
import { relativeTime } from "@/lib/format";
import { safeReturnTo } from "@/lib/security/return-to";
import type { NotificationPage } from "@/lib/types";

export function NotificationBell() {
  const [open, setOpen] = React.useState(false);
  const [page, setPage] = React.useState<NotificationPage | null>(null);
  const [unavailable, setUnavailable] = React.useState(false);
  const wrapper = React.useRef<HTMLDivElement>(null);
  const panelId = React.useId();

  const load = React.useCallback(async () => {
    try {
      setPage(await api<NotificationPage>("/api/v1/notifications?limit=10", { quietAuth: true }));
      setUnavailable(false);
    } catch {
      setUnavailable(true);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  useLiveEvents(() => void load(), { events: ["notification"], enabled: !unavailable });

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapper.current && !wrapper.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function markRead(id: string) {
    setPage((p) =>
      p
        ? {
            ...p,
            unread_count: Math.max(0, p.unread_count - (p.items.find((n) => n.id === id && !n.read_at) ? 1 : 0)),
            items: p.items.map((n) => (n.id === id ? { ...n, read_at: n.read_at ?? new Date().toISOString() } : n)),
          }
        : p,
    );
    try {
      await api(`/api/v1/notifications/${encodeURIComponent(id)}/read`, { method: "POST" });
    } catch {
      void load();
    }
  }

  async function markAll() {
    try {
      await api("/api/v1/notifications/read-all", { method: "POST" });
    } finally {
      void load();
    }
  }

  const unread = page?.unread_count ?? 0;
  return (
    <div ref={wrapper} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"}
        className="relative rounded-md p-1.5 hover:bg-surface-2"
      >
        <Bell className="h-5 w-5" aria-hidden />
        {unread > 0 ? (
          <span className="tabular absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-danger px-1 text-[10px] font-semibold text-white">
            {unread > 99 ? "99+" : unread}
          </span>
        ) : null}
      </button>
      {open ? (
        <div id={panelId} role="region" aria-label="Notifications" className="absolute right-0 z-40 mt-2 w-80 max-w-[calc(100vw-1.5rem)] rounded-lg border border-border bg-surface shadow-lg">
          <div className="flex items-center justify-between border-b border-border px-4 py-2">
            <p className="text-sm font-semibold">Notifications</p>
            {unread > 0 ? (
              <button type="button" onClick={markAll} className="text-xs text-brand hover:underline">
                Mark all read
              </button>
            ) : null}
          </div>
          {unavailable ? (
            <p className="px-4 py-6 text-center text-sm text-muted">Notifications are unavailable right now.</p>
          ) : !page ? (
            <p className="px-4 py-6 text-center text-sm text-muted">Loading…</p>
          ) : page.items.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-muted">You&apos;re all caught up.</p>
          ) : (
            <ul className="max-h-96 overflow-y-auto">
              {page.items.map((n) => {
                const href = n.link ? safeReturnTo(n.link, "") : "";
                const content = (
                  <>
                    <p className={cn("text-sm", !n.read_at && "font-medium")}>{n.title}</p>
                    {n.body ? <p className="mt-0.5 line-clamp-2 text-xs text-muted">{n.body}</p> : null}
                    <p className="mt-1 text-[11px] text-muted">{relativeTime(n.created_at)}</p>
                  </>
                );
                return (
                  <li key={n.id} className="border-b border-border last:border-b-0">
                    {href ? (
                      <Link
                        href={href}
                        onClick={() => {
                          void markRead(n.id);
                          setOpen(false);
                        }}
                        className="block px-4 py-2.5 hover:bg-surface-2"
                      >
                        {content}
                      </Link>
                    ) : (
                      <button type="button" onClick={() => void markRead(n.id)} className="block w-full px-4 py-2.5 text-left hover:bg-surface-2">
                        {content}
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
