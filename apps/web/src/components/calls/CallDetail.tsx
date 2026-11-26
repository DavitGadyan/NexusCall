"use client";
/**
 * Call detail: lifecycle timeline, outcome, AI session facts, and the transcript and recording. Transcript
 * and recording are fetched only when the user asks (each fetch is audited by voice-service); the
 * recording link is short-lived and is dropped from memory when it expires or the page closes.
 */
import { Download, FileText, PhoneOff, Play, ShieldAlert } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime } from "@/lib/format";
import { useCan } from "@/components/shell/session-context";
import { Badge, Button, Card, CardHeader, ConfirmDialog, ErrorState, KeyValues, LoadingState, Notice, PageHeader, useToast } from "@/components/ui";
import { useAgentNames } from "./pickers";
import { buildTimeline, callStateTone, formatCost, formatDuration, formatOffset, isLive, safeHttpUrl, secondsLeft } from "./shared";
import type { Call, HangupOut, Recording, Transcript, VoiceSession } from "./types";
import { useLiveRefresh, useNow } from "./use-live";

export function CallDetail({ callId }: { callId: string }) {
  const can = useCan();
  const toast = useToast();
  const call = useApi<Call>(`/api/v1/calls/${callId}`);
  const session = useApi<VoiceSession>(`/api/v1/voice/calls/${callId}`);
  const live = call.data ? isLive(call.data.state) : false;
  const reloadAll = React.useCallback(() => {
    call.reload();
    session.reload();
  }, [call, session]);
  useLiveRefresh(reloadAll, {
    events: ["call.updated"],
    match: (e) => e.type === "ui" && e.data.call_id === callId,
    pollMs: live ? 5000 : 0,
  });
  const agentName = useAgentNames();
  const [hangup, setHangup] = React.useState(false);

  if (call.loading && !call.data) return <Card><LoadingState label="Loading call" /></Card>;
  if (call.error && !call.data) return <Card><ErrorState problem={call.error.problem} onRetry={call.reload} /></Card>;
  const c = call.data;
  if (!c) return null;
  const sessionMissing = session.error?.status === 404;
  const s = session.data ?? null;
  const inbound = c.direction === "inbound";

  return (
    <>
      <PageHeader
        title={`${inbound ? "Inbound call from" : "Call to"} ${(inbound ? c.from_masked : c.to_masked) ?? "unknown number"}`}
        description={`${formatDateTime(c.created_at)} · ${c.is_test ? "Test call · " : ""}${c.direction}`}
        actions={
          live && can("calls:manage") ? (
            <Button variant="danger" onClick={() => setHangup(true)}>
              <PhoneOff className="h-4 w-4" aria-hidden /> Hang up
            </Button>
          ) : null
        }
      />
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="flex flex-col gap-4 lg:col-span-2">
          <Card>
            <CardHeader title="Outcome" />
            <div className="px-5 py-4">
              <KeyValues
                items={[
                  { label: "State", value: <Badge tone={callStateTone(c.state)}>{c.state.replace(/_/g, " ")}</Badge> },
                  { label: "Outcome", value: c.outcome ? c.outcome.replace(/_/g, " ") : <span className="text-muted">Not determined yet</span> },
                  { label: "Qualified", value: c.qualified === true ? "Yes" : c.qualified === false ? "No" : "—" },
                  { label: "End reason", value: c.end_reason?.replace(/_/g, " ") ?? "—" },
                  ...(c.blocked_reason ? [{ label: "Blocked before dialing", value: <span className="text-warn">{c.blocked_reason.replace(/_/g, " ")}</span> }] : []),
                  { label: "Opted out", value: c.opted_out ? <Badge tone="warn">Contact opted out during the call</Badge> : "No" },
                  { label: "Talk time", value: formatDuration(c.duration_seconds) },
                  { label: "Cost", value: formatCost(c.cost_micros) },
                ]}
              />
              <div className="mt-4 rounded-md border border-border bg-surface-2 px-4 py-3 text-sm">
                <p className="font-medium">Summary</p>
                <p className="mt-1 text-muted">
                  {s?.postprocess_status === "done"
                    ? "Post-call analysis finished; its outcome and qualification are shown above. A written call summary is not available in this view yet."
                    : s
                      ? `Post-call analysis: ${s.postprocess_status.replace(/_/g, " ")}.`
                      : "No AI session, so no summary."}
                </p>
              </div>
            </div>
          </Card>
          <MediaPanel call={c} session={s} canRead={can("calls:recordings:read")} />
        </div>
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader title="Details" />
            <div className="px-5 py-4">
              <KeyValues
                items={[
                  { label: "Agent", value: agentName(c.agent_id) },
                  { label: "Campaign", value: c.campaign_id ? <Link className="text-brand hover:underline" href={`/app/campaigns/calls/${c.campaign_id}`}>Open campaign</Link> : "—" },
                  { label: "Contact", value: c.contact_id ? <Link className="text-brand hover:underline" href={`/app/crm/contacts/${c.contact_id}`}>Open contact</Link> : "—" },
                  { label: "Attempts", value: <span className="tabular">{c.attempt_count}</span> },
                  { label: "Handoff", value: c.handoff_state?.replace(/_/g, " ") ?? "—" },
                  ...(s
                    ? [
                        { label: "AI disclosure", value: s.disclosure_played ? "Played" : <span className="text-warn">Not played</span> },
                        { label: "Recording consent prompt", value: s.consent_prompt_played ? "Played" : "Not played" },
                        { label: "Turns / barge-ins", value: <span className="tabular">{s.turns} / {s.barge_ins}</span> },
                      ]
                    : []),
                ]}
              />
              {sessionMissing ? <p className="mt-3 text-xs text-muted">No AI voice session was recorded for this call.</p> : null}
              {session.error && !sessionMissing ? <p className="mt-3 text-xs text-danger">Session details unavailable: {session.error.message}</p> : null}
            </div>
          </Card>
          <Card>
            <CardHeader title="Timeline" />
            <ol className="flex flex-col gap-0 px-5 py-4" aria-label="Call timeline">
              {buildTimeline(c, s).map((t, i) => (
                <li key={`${t.at}-${i}`} className="relative border-l border-border pb-4 pl-4 last:pb-0">
                  <span
                    aria-hidden
                    className={`absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full ${t.tone === "ok" ? "bg-ok" : t.tone === "danger" ? "bg-danger" : t.tone === "warn" ? "bg-warn" : "bg-muted"}`}
                  />
                  <p className="text-sm font-medium">{t.label}</p>
                  <p className="text-xs text-muted">
                    <time dateTime={t.at}>{formatDateTime(t.at)}</time>
                    {t.detail ? ` · ${t.detail}` : ""}
                  </p>
                </li>
              ))}
            </ol>
          </Card>
        </div>
      </div>
      <ConfirmDialog
        open={hangup}
        onClose={() => setHangup(false)}
        title="End this call now?"
        description="The other party hears the call end immediately. This action is recorded in the audit log."
        confirmLabel="Hang up"
        onConfirm={async () => {
          const out = await api<HangupOut>(`/api/v1/calls/${callId}/hangup`, { method: "POST" });
          toast({ tone: out.hangup === "not_live" ? "info" : "success", title: out.hangup === "not_live" ? "The call had already ended" : "Hang-up requested" });
          reloadAll();
        }}
      />
    </>
  );
}

function MediaPanel({ call, session, canRead }: { call: Call; session: VoiceSession | null; canRead: boolean }) {
  return (
    <Card>
      <CardHeader title="Transcript and recording" description="Sensitive content. Loaded only when you open it." />
      <div className="flex flex-col gap-4 px-5 py-4">
        <Notice tone="warn" title="Access is audited">
          Opening the transcript or the recording is recorded in the audit log with your name and the time. Open them only when you need to.
        </Notice>
        {!canRead ? (
          <p className="text-sm text-muted">You do not have permission to open transcripts or recordings.</p>
        ) : !session ? (
          <p className="text-sm text-muted">There is no AI voice session for this call, so there is no transcript or recording.</p>
        ) : (
          <>
            <RecordingPlayer callId={call.id} available={session.recording_available} />
            <TranscriptViewer callId={call.id} available={session.transcript_available} />
          </>
        )}
      </div>
    </Card>
  );
}

function RecordingPlayer({ callId, available }: { callId: string; available: boolean }) {
  const [rec, setRec] = React.useState<{ url: string; meta: Recording; fetchedAt: number } | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [playbackBlocked, setPlaybackBlocked] = React.useState(false);
  const now = useNow(1000, rec !== null);
  const left = rec ? secondsLeft(rec.fetchedAt, rec.meta.expires_in_seconds, now) : 0;

  React.useEffect(() => {
    if (rec && left <= 0) setRec(null); // never keep an expired link around
  }, [rec, left]);

  async function load() {
    setBusy(true);
    setError(null);
    setPlaybackBlocked(false);
    try {
      const meta = await api<Recording>(`/api/v1/voice/calls/${callId}/recording`);
      const url = safeHttpUrl(meta.url);
      if (!url) throw new Error("The recording link was not a valid download URL.");
      setRec({ url, meta, fetchedAt: Date.now() });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  if (!available) return <p className="text-sm text-muted">No recording is stored for this call (not recorded, or removed by the retention policy).</p>;
  return (
    <section aria-label="Recording" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">Recording</h3>
        {rec ? (
          <span className="tabular text-xs text-muted" aria-live="off">
            Link expires in {formatDuration(left)}
          </span>
        ) : null}
      </div>
      {rec ? (
        <>
          <audio controls preload="none" src={rec.url} className="w-full" onError={() => setPlaybackBlocked(true)}>
            Your browser cannot play this recording.
          </audio>
          <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
            <span>
              {formatDuration(rec.meta.duration_seconds)} · {rec.meta.channels === 2 ? "dual channel" : "mono"} · {rec.meta.content_type}
            </span>
            <a href={rec.url} rel="noreferrer noopener" className="inline-flex items-center gap-1 text-brand hover:underline">
              <Download className="h-3.5 w-3.5" aria-hidden /> Download
            </a>
            <button type="button" className="text-brand hover:underline" onClick={() => setRec(null)}>
              Close
            </button>
          </div>
          {playbackBlocked ? <p className="text-xs text-warn">Playback failed in the browser. Use Download instead, or load a fresh link.</p> : null}
        </>
      ) : (
        <div>
          <Button size="sm" variant="secondary" onClick={load} disabled={busy} data-testid="load-recording">
            <Play className="h-4 w-4" aria-hidden /> {busy ? "Requesting link…" : "Load recording"}
          </Button>
          <p className="mt-1 text-xs text-muted">Creates a private link valid for up to 15 minutes. Each request is audited.</p>
        </div>
      )}
      {error ? <p className="text-xs text-danger">{error.message}</p> : null}
    </section>
  );
}

function TranscriptViewer({ callId, available }: { callId: string; available: boolean }) {
  const [data, setData] = React.useState<Transcript | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  async function load() {
    setBusy(true);
    setError(null);
    try {
      setData(await api<Transcript>(`/api/v1/voice/calls/${callId}/transcript`));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  if (!available) return <p className="text-sm text-muted">The transcript is no longer available (removed by the retention policy).</p>;
  return (
    <section aria-label="Transcript" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">Transcript</h3>
        {data ? (
          <button type="button" className="text-xs text-brand hover:underline" onClick={() => setData(null)}>
            Hide transcript
          </button>
        ) : null}
      </div>
      {data ? (
        data.segments.length === 0 ? (
          <p className="text-sm text-muted">The transcript is empty.</p>
        ) : (
          <ol className="flex max-h-[28rem] flex-col gap-2 overflow-y-auto rounded-md border border-border p-3" data-testid="transcript">
            {data.segments.map((seg) => {
              const ai = seg.role === "assistant" || seg.role === "agent" || seg.role === "ai";
              return (
                <li key={seg.seq} className={`flex flex-col gap-0.5 ${ai ? "items-start" : "items-end"}`}>
                  <span className="text-[11px] text-muted">
                    {ai ? "AI agent" : seg.role === "user" || seg.role === "caller" ? "Caller" : seg.role} · {formatOffset(seg.started_ms)}
                    {seg.interrupted ? " · interrupted" : ""}
                  </span>
                  <p className={`max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-1.5 text-sm ${ai ? "bg-brand/10" : "bg-surface-2"}`}>{seg.text}</p>
                </li>
              );
            })}
          </ol>
        )
      ) : (
        <div>
          <Button size="sm" variant="secondary" onClick={load} disabled={busy} data-testid="load-transcript">
            <FileText className="h-4 w-4" aria-hidden /> {busy ? "Loading…" : "Show transcript"}
          </Button>
        </div>
      )}
      {error ? (
        <p className="inline-flex items-center gap-1 text-xs text-danger">
          <ShieldAlert className="h-3.5 w-3.5" aria-hidden /> {error.message}
        </p>
      ) : null}
    </section>
  );
}
