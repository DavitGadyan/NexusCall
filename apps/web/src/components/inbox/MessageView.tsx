"use client";
/**
 * One email message. HTML bodies arrive sanitized from the server and are still rendered only through
 * `SafeHtmlFrame` (DOMPurify + a fully sandboxed iframe, no remote images). Attachments are downloadable only
 * when scanned clean; downloads use a short-lived link fetched on click (audited by the service).
 */
import { Bot, Download, FileWarning, Paperclip, ShieldX } from "lucide-react";
import * as React from "react";
import { api, toApiError } from "@/lib/client/api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/cn";
import { Badge, SafeHtmlFrame, useToast } from "@/components/ui";
import { safeHttpUrl } from "@/components/calls/shared";
import { attachmentState, formatBytes } from "./logic";
import type { Attachment, InboxMessage } from "./types";

export function MessageView({ message, compact, highlight }: { message: InboxMessage; compact?: boolean; highlight?: boolean }) {
  const outbound = message.direction === "outbound";
  const [showText, setShowText] = React.useState(false);
  const hasHtml = Boolean(message.html_sanitized) && !message.content_purged;
  return (
    <article
      className={cn("rounded-lg border bg-surface", highlight ? "border-brand/60 ring-1 ring-brand/30" : "border-border", outbound && "bg-surface-2/50")}
      aria-label={`${outbound ? "Sent" : "Received"} message from ${message.from_name || message.from_address}`}
    >
      <header className="flex flex-wrap items-start justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0 text-sm">
          <p className="truncate">
            <span className="font-medium">{message.from_name || message.from_address}</span>{" "}
            {message.from_name ? <span className="text-muted">&lt;{message.from_address}&gt;</span> : null}
          </p>
          <p className="truncate text-xs text-muted">
            To {message.to_addresses.join(", ") || "—"}
            {message.cc_addresses.length ? ` · Cc ${message.cc_addresses.join(", ")}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {outbound && message.draft_id ? (
            <Badge tone="brand">
              <Bot className="mr-1 h-3 w-3" aria-hidden /> AI-drafted, approved v{message.draft_version}
            </Badge>
          ) : outbound ? (
            <Badge>Sent</Badge>
          ) : null}
          {message.auto_flags.map((f) => (
            <Badge key={f} tone="warn">
              {f.replace(/_/g, " ")}
            </Badge>
          ))}
          <time className="text-xs text-muted" dateTime={message.received_at}>
            {formatDateTime(message.received_at)}
          </time>
        </div>
      </header>
      <div className="px-4 py-3">
        {!compact ? <p className="mb-2 text-sm font-medium">{message.subject}</p> : null}
        {message.content_purged ? (
          <p className="text-sm text-muted">The content of this message was removed by the retention policy.</p>
        ) : hasHtml && !showText ? (
          <SafeHtmlFrame html={message.html_sanitized ?? ""} title={`Email body: ${message.subject}`} minHeight={compact ? 160 : 240} />
        ) : (
          <pre className="max-h-[480px] overflow-auto whitespace-pre-wrap break-words font-sans text-sm">{message.text_body || "(no text)"}</pre>
        )}
        {hasHtml ? (
          <button type="button" className="mt-2 text-xs text-brand hover:underline" onClick={() => setShowText((v) => !v)}>
            {showText ? "Show formatted" : "Show plain text"}
          </button>
        ) : null}
      </div>
      {message.attachments && message.attachments.length > 0 ? <Attachments items={message.attachments} /> : null}
    </article>
  );
}

function Attachments({ items }: { items: Attachment[] }) {
  const toast = useToast();
  const [busy, setBusy] = React.useState<string | null>(null);
  async function download(a: Attachment) {
    setBusy(a.id);
    try {
      const out = await api<{ url: string; expires_in_seconds: number }>(`/api/v1/email/inbox/attachments/${a.id}/download`);
      const url = safeHttpUrl(out.url);
      if (!url) throw new Error("The download link was not valid.");
      // The object store answers with Content-Disposition: attachment, so the page stays where it is.
      window.location.assign(url);
    } catch (err) {
      const e = toApiError(err);
      if (!e.isStepUp) toast({ tone: "error", title: "Download not available", description: e.message });
    } finally {
      setBusy(null);
    }
  }
  return (
    <ul className="flex flex-col gap-1.5 border-t border-border px-4 py-3" aria-label="Attachments">
      {items.map((a) => {
        const st = attachmentState(a);
        return (
          <li key={a.id} className="flex flex-wrap items-center justify-between gap-2 text-sm" data-testid="attachment">
            <span className="inline-flex min-w-0 items-center gap-2">
              {st.kind === "blocked" ? <ShieldX className="h-4 w-4 shrink-0 text-danger" aria-hidden /> : st.kind === "unavailable" ? <FileWarning className="h-4 w-4 shrink-0 text-warn" aria-hidden /> : <Paperclip className="h-4 w-4 shrink-0 text-muted" aria-hidden />}
              <span className="truncate">{a.filename}</span>
              <span className="shrink-0 text-xs text-muted">
                {formatBytes(a.size_bytes)} · {a.content_type}
              </span>
            </span>
            <span className="inline-flex items-center gap-2">
              <Badge tone={st.tone}>{st.label}</Badge>
              {st.kind === "download" ? (
                <button
                  type="button"
                  onClick={() => download(a)}
                  disabled={busy === a.id}
                  className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-2 disabled:opacity-60"
                  aria-label={`Download ${a.filename}`}
                >
                  <Download className="h-3.5 w-3.5" aria-hidden /> {busy === a.id ? "Preparing…" : "Download"}
                </button>
              ) : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
