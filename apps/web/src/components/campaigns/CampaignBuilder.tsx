"use client";
/**
 * Campaign builder (create and edit): agent, segment, timezone, calling window, pacing and attempts,
 * validated in the browser against the form rules and — when the user may read them — the workspace limits.
 * Schedule and launch re-validate on the server and return the remaining problems.
 */
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError, withQuery } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { Button, ButtonLink, Card, CardHeader, Checkbox, ErrorState, Field, Input, LoadingState, Notice, PageHeader, Select, Textarea, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { MutationError } from "@/components/crm/common";
import { newIdempotencyKey } from "@/components/crm/lib/http";
import type { Items, Segment, SegmentPreview } from "@/components/crm/lib/types";
import type { Agent } from "@/components/agents/lib/types";
import {
  buildCampaign,
  type Campaign,
  type CampaignForm,
  campaignPatch,
  campaignToForm,
  DAY_LABEL,
  DAYS,
  defaultForm,
  editableFields,
  limitProblems,
  problemLabel,
  type TenantLimits,
} from "./lib/campaign-form";

function timezones(): string[] {
  try {
    const list = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone");
    if (list && list.length) return list.includes("UTC") ? list : ["UTC", ...list];
  } catch {
    // older browsers
  }
  return ["UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Tokyo", "Australia/Sydney"];
}

function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function CampaignBuilder({ campaignId }: { campaignId: string | null }) {
  const can = useCan();
  const router = useRouter();
  const toast = useToast();
  const existing = useApi<Campaign>(campaignId ? `/api/v1/campaigns/${campaignId}` : null);
  const agents = useApi<Items<Agent>>(can("ai:agents:read") ? withQuery("/api/v1/ai/agents", { kind: "voice", limit: 100 }) : null);
  const segments = useApi<Items<Segment>>("/api/v1/segments");
  const limits = useApi<TenantLimits>(can("tenant:manage") ? "/api/v1/settings/communication" : null);
  const [form, setForm] = React.useState<CampaignForm>(() => defaultForm(browserZone()));
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef(newIdempotencyKey());
  const loaded = React.useRef<number | null>(null);
  const zones = React.useMemo(timezones, []);

  React.useEffect(() => {
    if (existing.data && loaded.current !== existing.data.version) {
      loaded.current = existing.data.version;
      setForm(campaignToForm(existing.data));
    }
  }, [existing.data]);

  const audience = useApi<SegmentPreview>(form.segment_id ? withQuery(`/api/v1/segments/${form.segment_id}/preview`, { channel: "phone" }) : null);
  const built = React.useMemo(() => buildCampaign(form), [form]);
  const limitIssues = built.body ? limitProblems(built.body, limits.data) : [];

  if (campaignId && existing.loading && !existing.data) return <Card><LoadingState /></Card>;
  if (campaignId && existing.error && !existing.data) return <Card><ErrorState problem={existing.error.problem} onRetry={existing.reload} /></Card>;
  const editable = existing.data ? editableFields(existing.data.state) : "all";
  const locked = (f: string) => editable === "none" || (editable !== "all" && !editable.has(f));
  const set = <K extends keyof CampaignForm>(k: K, v: CampaignForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const err = (k: string) => errors[k] ?? error?.fields[k] ?? (k === "retry_delay_minutes" ? error?.fields.retry_delay_seconds : undefined);
  const selectedAgent = agents.data?.items.find((a) => a.id === form.agent_id);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const b = buildCampaign(form);
    setErrors(b.errors);
    setError(null);
    if (!b.body) return;
    setBusy(true);
    try {
      if (existing.data) {
        const patch = campaignPatch(existing.data, b.body);
        if (Object.keys(patch).length) await api<Campaign>(`/api/v1/campaigns/${existing.data.id}`, { method: "PATCH", body: patch });
        toast({ tone: "success", title: "Campaign saved" });
        router.push(`/app/campaigns/calls/${existing.data.id}`);
      } else {
        const created = await api<Campaign>("/api/v1/campaigns", { method: "POST", body: b.body, idempotencyKey: key.current });
        toast({ tone: "success", title: "Campaign created", description: "Schedule it when it is ready." });
        router.push(`/app/campaigns/calls/${created.id}`);
      }
    } catch (err2) {
      setError(toApiError(err2));
    } finally {
      setBusy(false);
    }
  }

  const backHref = existing.data ? `/app/campaigns/calls/${existing.data.id}` : "/app/campaigns/calls";
  return (
    <>
      <PageHeader title={existing.data ? `Edit ${existing.data.name}` : "New call campaign"} description="Contacts are called in their own timezone when known, otherwise in the campaign timezone, only inside the window." />
      {existing.data && editable === "none" ? <div className="mb-4"><Notice tone="warn" title={`A ${existing.data.state} campaign cannot be edited`} /></div> : null}
      {existing.data?.state === "paused" ? <div className="mb-4"><Notice tone="info">While paused, only the window, pacing, end time and description can change.</Notice></div> : null}
      <form onSubmit={submit} className="grid gap-4 lg:grid-cols-3" noValidate>
        <div className="flex flex-col gap-4 lg:col-span-2">
          <Card>
            <CardHeader title="Basics" />
            <div className="grid gap-3 px-5 py-4 sm:grid-cols-2">
              <Field id="cmp-name" label="Name" required error={err("name")} className="sm:col-span-2">
                {(p) => <Input {...p} maxLength={200} value={form.name} disabled={locked("name")} onChange={(e) => set("name", e.target.value)} />}
              </Field>
              <Field id="cmp-desc" label="Description" error={err("description")} className="sm:col-span-2">
                {(p) => <Textarea {...p} rows={2} maxLength={1000} value={form.description} disabled={locked("description")} onChange={(e) => set("description", e.target.value)} />}
              </Field>
              <Field id="cmp-agent" label="AI voice agent" hint={selectedAgent && selectedAgent.status !== "active" ? "This agent has no active version yet; launch will be refused." : "Only voice agents can call."} error={err("agent_id")}>
                {(p) =>
                  agents.data || agents.loading ? (
                    <Select {...p} value={form.agent_id} disabled={locked("agent_id")} onChange={(e) => set("agent_id", e.target.value)}>
                      <option value="">{agents.loading ? "Loading…" : "Choose an agent…"}</option>
                      {(agents.data?.items ?? []).filter((a) => a.status !== "archived").map((a) => (
                        <option key={a.id} value={a.id}>{a.name}{a.status !== "active" ? ` (${a.status})` : ""}</option>
                      ))}
                    </Select>
                  ) : (
                    <Input {...p} placeholder="Agent ID" value={form.agent_id} disabled={locked("agent_id")} onChange={(e) => set("agent_id", e.target.value.trim())} />
                  )
                }
              </Field>
              <Field id="cmp-segment" label="Segment" hint={audience.data ? `${audience.data.matching.toLocaleString()} matching, ${audience.data.contactable?.toLocaleString() ?? "?"} callable now.` : "The audience is snapshotted at launch."} error={err("segment_id")}>
                {(p) => (
                  <Select {...p} value={form.segment_id} disabled={locked("segment_id")} onChange={(e) => set("segment_id", e.target.value)}>
                    <option value="">{segments.loading ? "Loading…" : "Choose a segment…"}</option>
                    {(segments.data?.items ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </Select>
                )}
              </Field>
            </div>
          </Card>
          <Card>
            <CardHeader title="Calling window" />
            <div className="grid gap-3 px-5 py-4 sm:grid-cols-3">
              <Field id="cmp-tz" label="Campaign timezone" error={err("timezone")}>
                {(p) => <Select {...p} value={form.timezone} disabled={locked("timezone")} onChange={(e) => set("timezone", e.target.value)}>{zones.map((z) => <option key={z} value={z}>{z}</option>)}</Select>}
              </Field>
              <Field id="cmp-ws" label="From" error={err("window_start")}>
                {(p) => <Input {...p} type="time" value={form.window_start} disabled={locked("window_start")} onChange={(e) => set("window_start", e.target.value)} />}
              </Field>
              <Field id="cmp-we" label="To" error={err("window_end")}>
                {(p) => <Input {...p} type="time" value={form.window_end} disabled={locked("window_end")} onChange={(e) => set("window_end", e.target.value)} />}
              </Field>
              <fieldset className="sm:col-span-3">
                <legend className="mb-1 text-sm font-medium">Days</legend>
                <div className="flex flex-wrap gap-3">
                  {DAYS.map((d) => (
                    <Checkbox key={d} label={DAY_LABEL[d]} checked={form.window_days.includes(d)} disabled={locked("window_days")} onChange={(e) => set("window_days", e.target.checked ? [...form.window_days, d] : form.window_days.filter((x) => x !== d))} />
                  ))}
                </div>
                {err("window_days") ? <p className="mt-1 text-xs text-danger">{err("window_days")}</p> : null}
              </fieldset>
              <Field id="cmp-start" label="Start (optional)" hint="Your local time." error={err("start_at")}>
                {(p) => <Input {...p} type="datetime-local" value={form.start_at} disabled={locked("start_at")} onChange={(e) => set("start_at", e.target.value)} />}
              </Field>
              <Field id="cmp-end" label="End (optional)" hint="Your local time." error={err("end_at")}>
                {(p) => <Input {...p} type="datetime-local" value={form.end_at} disabled={locked("end_at")} onChange={(e) => set("end_at", e.target.value)} />}
              </Field>
            </div>
          </Card>
          <Card>
            <CardHeader title="Pacing and attempts" />
            <div className="grid gap-3 px-5 py-4 sm:grid-cols-2 lg:grid-cols-4">
              <Field id="cmp-cpm" label="Calls per minute" hint="1–600" error={err("calls_per_minute")}>
                {(p) => <Input {...p} inputMode="numeric" value={form.calls_per_minute} disabled={locked("calls_per_minute")} onChange={(e) => set("calls_per_minute", e.target.value)} />}
              </Field>
              <Field id="cmp-conc" label="Concurrent calls" hint={limits.data?.max_concurrent_calls ? `Workspace limit ${limits.data.max_concurrent_calls}` : "1–1000"} error={err("max_concurrency")}>
                {(p) => <Input {...p} inputMode="numeric" value={form.max_concurrency} disabled={locked("max_concurrency")} onChange={(e) => set("max_concurrency", e.target.value)} />}
              </Field>
              <Field id="cmp-att" label="Attempts per contact" hint="1–10" error={err("max_attempts")}>
                {(p) => <Input {...p} inputMode="numeric" value={form.max_attempts} disabled={locked("max_attempts")} onChange={(e) => set("max_attempts", e.target.value)} />}
              </Field>
              <Field id="cmp-retry" label="Retry after (minutes)" hint="1–10080" error={err("retry_delay_minutes")}>
                {(p) => <Input {...p} inputMode="numeric" value={form.retry_delay_minutes} disabled={locked("retry_delay_seconds")} onChange={(e) => set("retry_delay_minutes", e.target.value)} />}
              </Field>
            </div>
          </Card>
          <MutationError error={error} onReload={() => { setError(null); loaded.current = null; existing.reload(); }} />
          <div className="flex justify-end gap-2">
            <ButtonLink href={backHref}>Cancel</ButtonLink>
            <Button type="submit" disabled={busy || editable === "none"} data-testid="save-campaign">{busy ? "Saving…" : existing.data ? "Save changes" : "Create draft"}</Button>
          </div>
        </div>
        <Card className="h-fit lg:sticky lg:top-4">
          <CardHeader title="Workspace limits" />
          <div className="flex flex-col gap-3 px-5 py-4 text-sm">
            {limits.data ? (
              <ul className="flex flex-col gap-1 text-muted">
                <li>Concurrent calls: <span className="tabular text-fg">{limits.data.max_concurrent_calls ?? "—"}</span></li>
                <li>Calls per day: <span className="tabular text-fg">{limits.data.max_daily_calls ?? "—"}</span></li>
                <li>Calling window: <span className="text-fg">{limits.data.calling_window ? `${limits.data.calling_window.start ?? "00:00"}–${limits.data.calling_window.end ?? "23:59"} ${(limits.data.calling_window.days ?? DAYS).join(", ")}` : "not set"}</span></li>
                <li>Calls enabled: <span className="text-fg">{limits.data.calls_enabled === false ? "no" : "yes"}</span></li>
              </ul>
            ) : (
              <p className="text-muted">{limits.loading ? "Loading limits…" : "Your role cannot read the workspace limits. They are checked when you schedule and launch."}</p>
            )}
            {limitIssues.length ? (
              <Notice tone="warn" title="Launch will be refused">
                <ul className="list-disc pl-4">{limitIssues.map((p) => <li key={p}>{problemLabel(p)}</li>)}</ul>
              </Notice>
            ) : limits.data && built.body ? <Notice tone="ok">Within the workspace limits.</Notice> : null}
          </div>
        </Card>
      </form>
    </>
  );
}
