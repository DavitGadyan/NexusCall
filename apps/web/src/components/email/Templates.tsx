"use client";
/**
 * Email templates: list, create, and the editor. Saving appends a new version (campaigns keep the version
 * they pinned). Previews are rendered by email-service and shown through the sanitizer in a sandboxed frame.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import type { Page } from "@/lib/types";
import { useCan } from "@/components/shell/session-context";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  type Column,
  ConflictNotice,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  FormError,
  Input,
  LoadingState,
  Notice,
  PageHeader,
  SafeHtmlFrame,
  Select,
  Tabs,
  Textarea,
  useToast,
} from "@/components/ui";
import { useIdempotencyKey } from "@/components/calls/use-live";
import { senderBlockReason, TEMPLATE_VARIABLES, templateProblems } from "./logic";
import type { Preview, Sender, Template, TemplateVersion } from "./types";

function ProblemList({ error }: { error: ApiError | null }) {
  if (!error) return null;
  const problems = templateProblems(error.problem as Record<string, unknown>);
  if (problems.length === 0) return null;
  return (
    <Notice tone="danger" title="The template was not accepted">
      <ul className="list-disc pl-5">
        {problems.map((p) => (
          <li key={p}>{p}</li>
        ))}
      </ul>
    </Notice>
  );
}

export function TemplateList() {
  const can = useCan();
  const router = useRouter();
  const list = useCursorList<Template>("/api/v1/email/templates");
  const [creating, setCreating] = React.useState(false);
  const columns: Column<Template>[] = [
    {
      key: "name",
      header: "Name",
      cell: (t) => (
        <Link href={`/app/campaigns/email/templates/${t.id}`} className="font-medium text-brand hover:underline">
          {t.name}
        </Link>
      ),
    },
    { key: "category", header: "Kind", cell: (t) => <Badge tone={t.category === "campaign" ? "brand" : "neutral"}>{t.category}</Badge> },
    { key: "subject", header: "Subject", cell: (t) => <span className="line-clamp-1">{t.current?.subject ?? "—"}</span>, hideOnMobile: true },
    { key: "version", header: "Version", cell: (t) => <span className="tabular">v{t.current_version}</span> },
    { key: "updated", header: "Updated", cell: (t) => <span title={formatDateTime(t.updated_at)}>{relativeTime(t.updated_at)}</span>, hideOnMobile: true },
  ];
  return (
    <>
      <Card>
        <CardHeader
          title="Templates"
          description="Campaign templates always carry an unsubscribe link. Transactional templates are for one-off messages and cannot be used in campaigns."
          actions={
            can("emails:campaigns:manage") ? (
              <Button size="sm" onClick={() => setCreating(true)} data-testid="new-template">
                New template
              </Button>
            ) : null
          }
        />
        <DataTable
          caption="Email templates"
          columns={columns}
          rows={list.items}
          rowKey={(t) => t.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          onLoadMore={list.loadMore}
          empty={<EmptyState title="No templates yet" description="Create a template, preview it and send yourself a test." />}
        />
      </Card>
      <CreateTemplateDialog open={creating} onClose={() => setCreating(false)} onCreated={(t) => router.push(`/app/campaigns/email/templates/${t.id}`)} />
    </>
  );
}

const STARTER_HTML = '<p>Hi {{ first_name }},</p>\n<p>…</p>\n<p>— {{ sender_name }}</p>';

function CreateTemplateDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (t: Template) => void }) {
  const key = useIdempotencyKey(open);
  const [name, setName] = React.useState("");
  const [category, setCategory] = React.useState<"campaign" | "transactional">("campaign");
  const [subject, setSubject] = React.useState("");
  const [html, setHtml] = React.useState(STARTER_HTML);
  const [text, setText] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    if (open) {
      setName("");
      setCategory("campaign");
      setSubject("");
      setHtml(STARTER_HTML);
      setText("");
      setError(null);
    }
  }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(await api<Template>("/api/v1/email/templates", { method: "POST", body: { name: name.trim(), category, subject, html, text: text || null }, idempotencyKey: key || true }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  const problems = error ? templateProblems(error.problem as Record<string, unknown>) : [];
  return (
    <Dialog open={open} onClose={onClose} busy={busy} size="xl" title="New template" description={`Variables: ${TEMPLATE_VARIABLES.map((v) => `{{ ${v} }}`).join(", ")}.`}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="tpl-name" label="Name" required error={error?.fields.name ?? (error?.isConflict ? error.message : undefined)}>
            {(p) => <Input {...p} value={name} maxLength={120} onChange={(e) => setName(e.target.value)} />}
          </Field>
          <Field id="tpl-cat" label="Kind">
            {(p) => (
              <Select {...p} value={category} onChange={(e) => setCategory(e.target.value as "campaign" | "transactional")}>
                <option value="campaign">Campaign (marketing, with unsubscribe)</option>
                <option value="transactional">Transactional</option>
              </Select>
            )}
          </Field>
        </div>
        <TemplateFields subject={subject} html={html} text={text} onSubject={setSubject} onHtml={setHtml} onText={setText} error={error} />
        <ProblemList error={error} />
        <FormError message={error && problems.length === 0 && !error.isConflict && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !name.trim() || !subject.trim() || !html.trim()}>
            {busy ? "Creating…" : "Create template"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function TemplateFields({
  subject,
  html,
  text,
  onSubject,
  onHtml,
  onText,
  error,
  disabled,
}: {
  subject: string;
  html: string;
  text: string;
  onSubject: (v: string) => void;
  onHtml: (v: string) => void;
  onText: (v: string) => void;
  error: ApiError | null;
  disabled?: boolean;
}) {
  return (
    <>
      <Field id="tpl-subject" label="Subject" required error={error?.fields.subject}>
        {(p) => <Input {...p} value={subject} maxLength={998} disabled={disabled} onChange={(e) => onSubject(e.target.value)} />}
      </Field>
      <Field id="tpl-html" label="HTML body" required hint="Scripts, forms, styles from other sites and tracking pixels are removed when saved." error={error?.fields.html}>
        {(p) => <Textarea {...p} rows={12} value={html} disabled={disabled} onChange={(e) => onHtml(e.target.value)} className="font-mono text-xs" spellCheck={false} />}
      </Field>
      <Field id="tpl-text" label="Plain-text body (optional)" hint="Generated from the HTML when empty." error={error?.fields.text}>
        {(p) => <Textarea {...p} rows={5} value={text} disabled={disabled} onChange={(e) => onText(e.target.value)} className="font-mono text-xs" />}
      </Field>
    </>
  );
}

export function TemplateEditor({ templateId }: { templateId: string }) {
  const can = useCan();
  const toast = useToast();
  const tpl = useApi<Template>(`/api/v1/email/templates/${templateId}`);
  const versions = useApi<{ items: TemplateVersion[] }>(`/api/v1/email/templates/${templateId}/versions`);
  const [subject, setSubject] = React.useState("");
  const [html, setHtml] = React.useState("");
  const [text, setText] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [tab, setTab] = React.useState("edit");
  const [testOpen, setTestOpen] = React.useState(false);
  const manage = can("emails:campaigns:manage");

  const current = tpl.data?.current ?? null;
  React.useEffect(() => {
    if (current) {
      setSubject(current.subject);
      setHtml(current.html);
      setText(current.text ?? "");
    }
  }, [current]);

  if (tpl.loading && !tpl.data) return <Card><LoadingState label="Loading template" /></Card>;
  if (tpl.error && !tpl.data) return <Card><ErrorState problem={tpl.error.problem} onRetry={tpl.reload} /></Card>;
  const t = tpl.data;
  if (!t) return null;
  const dirty = current ? subject !== current.subject || html !== current.html || text !== (current.text ?? "") : false;

  async function save() {
    if (!t) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<Template>(`/api/v1/email/templates/${templateId}`, { method: "PUT", body: { expected_version: t.version, subject, html, text: text || null } });
      toast({ tone: "success", title: `Saved as version ${out.current_version}` });
      tpl.reload();
      versions.reload();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const problems = error ? templateProblems(error.problem as Record<string, unknown>) : [];
  return (
    <>
      <PageHeader
        title={t.name}
        description={`${t.category === "campaign" ? "Campaign" : "Transactional"} template · current version ${t.current_version} · updated ${relativeTime(t.updated_at)}`}
        actions={
          manage ? (
            <Button variant="secondary" onClick={() => setTestOpen(true)} data-testid="open-test-send">
              Send a test
            </Button>
          ) : null
        }
      />
      <Tabs
        label="Template"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "edit", label: "Edit" },
          { id: "preview", label: "Preview" },
          { id: "versions", label: "Versions", badge: <Badge>{versions.data?.items.length ?? t.current_version}</Badge> },
        ]}
      >
        {tab === "edit" ? (
          <Card className="flex flex-col gap-4 p-5">
            {error?.isConflict ? (
              <ConflictNotice
                onReload={() => {
                  setError(null);
                  tpl.reload();
                  versions.reload();
                }}
              />
            ) : null}
            <TemplateFields subject={subject} html={html} text={text} onSubject={setSubject} onHtml={setHtml} onText={setText} error={error} disabled={!manage} />
            <ProblemList error={error} />
            <FormError message={error && problems.length === 0 && !error.isConflict && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
            {manage ? (
              <div className="flex items-center justify-end gap-2">
                {dirty ? <span className="text-xs text-warn">Unsaved changes</span> : null}
                <Button
                  variant="secondary"
                  disabled={!dirty || busy}
                  onClick={() => {
                    if (current) {
                      setSubject(current.subject);
                      setHtml(current.html);
                      setText(current.text ?? "");
                    }
                  }}
                >
                  Discard
                </Button>
                <Button onClick={save} disabled={!dirty || busy || !subject.trim() || !html.trim()}>
                  {busy ? "Saving…" : "Save as new version"}
                </Button>
              </div>
            ) : null}
          </Card>
        ) : tab === "preview" ? (
          <PreviewPanel templateId={templateId} versions={versions.data?.items ?? []} current={t.current_version} dirty={dirty} />
        ) : (
          <VersionsPanel versions={versions.data?.items ?? []} loading={versions.loading} error={versions.error} onRetry={versions.reload} current={t.current_version} />
        )}
      </Tabs>
      <TestSendDialog open={testOpen} onClose={() => setTestOpen(false)} templateId={templateId} version={t.current_version} />
    </>
  );
}

function VariablesInputs({ values, onChange }: { values: Record<string, string>; onChange: (v: Record<string, string>) => void }) {
  return (
    <fieldset className="grid gap-3 sm:grid-cols-2">
      <legend className="mb-1 text-sm font-medium">Sample values (optional)</legend>
      {TEMPLATE_VARIABLES.map((v) => (
        <Field key={v} id={`var-${v}`} label={<span className="font-mono text-xs">{`{{ ${v} }}`}</span>}>
          {(p) => <Input {...p} value={values[v] ?? ""} maxLength={200} onChange={(e) => onChange({ ...values, [v]: e.target.value })} />}
        </Field>
      ))}
    </fieldset>
  );
}

function cleanVars(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim() !== ""));
}

function PreviewPanel({ templateId, versions, current, dirty }: { templateId: string; versions: TemplateVersion[]; current: number; dirty: boolean }) {
  const [version, setVersion] = React.useState(current);
  const [vars, setVars] = React.useState<Record<string, string>>({});
  const [preview, setPreview] = React.useState<Preview | null>(null);
  const [view, setView] = React.useState<"html" | "text">("html");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  async function render() {
    setBusy(true);
    setError(null);
    try {
      setPreview(await api<Preview>(`/api/v1/email/templates/${templateId}/preview`, { method: "POST", body: { version, variables: cleanVars(vars) } }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
      <Card className="flex flex-col gap-4 p-5">
        {dirty ? <Notice tone="warn">The preview shows saved versions. Save your changes to preview them.</Notice> : null}
        <Field id="pv-version" label="Version">
          {(p) => (
            <Select {...p} value={version} onChange={(e) => setVersion(Number(e.target.value))}>
              {(versions.length ? versions.map((v) => v.version) : [current]).map((n) => (
                <option key={n} value={n}>
                  v{n}
                  {n === current ? " (current)" : ""}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <VariablesInputs values={vars} onChange={setVars} />
        <Button onClick={render} disabled={busy} data-testid="render-preview">
          {busy ? "Rendering…" : "Render preview"}
        </Button>
        <FormError message={error && !error.isStepUp ? error.message : null} />
      </Card>
      <Card className="flex min-w-0 flex-col gap-3 p-5">
        {preview ? (
          <>
            <p className="text-sm">
              <span className="text-muted">Subject: </span>
              <span className="font-medium">{preview.subject}</span>
            </p>
            <div className="inline-flex w-fit rounded-md border border-border" role="group" aria-label="Preview format">
              {(["html", "text"] as const).map((v) => (
                <button key={v} type="button" aria-pressed={view === v} onClick={() => setView(v)} className={`px-3 py-1 text-sm ${view === v ? "bg-surface-2 font-medium" : "text-muted"}`}>
                  {v === "html" ? "HTML" : "Plain text"}
                </button>
              ))}
            </div>
            {view === "html" ? (
              <SafeHtmlFrame html={preview.html} title={`Preview of version ${preview.version}`} minHeight={420} />
            ) : (
              <pre className="max-h-[480px] overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 p-3 text-sm">{preview.text}</pre>
            )}
            <p className="text-xs text-muted">Rendered with sample values. Remote images are blocked in the preview. The unsubscribe link points to a placeholder.</p>
          </>
        ) : (
          <EmptyState title="No preview yet" description="Choose a version and render it with sample values." />
        )}
      </Card>
    </div>
  );
}

function VersionsPanel({ versions, loading, error, onRetry, current }: { versions: TemplateVersion[]; loading: boolean; error?: ApiError; onRetry: () => void; current: number }) {
  const [open, setOpen] = React.useState<number | null>(null);
  if (loading && versions.length === 0) return <Card><LoadingState label="Loading versions" /></Card>;
  if (error && versions.length === 0) return <Card><ErrorState problem={error.problem} onRetry={onRetry} /></Card>;
  return (
    <Card>
      <CardHeader title="Version history" description="Versions are never changed after saving. Campaigns keep the version they were launched with." />
      <ul className="divide-y divide-border">
        {versions.map((v) => (
          <li key={v.version} className="px-5 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="text-sm font-medium">
                  v{v.version} {v.version === current ? <Badge tone="brand">current</Badge> : null}
                </p>
                <p className="truncate text-xs text-muted">
                  {formatDateTime(v.created_at)} · {v.subject}
                </p>
              </div>
              <Button size="sm" variant="ghost" aria-expanded={open === v.version} onClick={() => setOpen(open === v.version ? null : v.version)}>
                {open === v.version ? "Hide source" : "Show source"}
              </Button>
            </div>
            {open === v.version ? (
              <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 p-3 font-mono text-xs">{v.html}</pre>
            ) : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function TestSendDialog({ open, onClose, templateId, version }: { open: boolean; onClose: () => void; templateId: string; version: number }) {
  const toast = useToast();
  const key = useIdempotencyKey(open);
  const senders = useApi<Page<Sender>>(open ? "/api/v1/email/senders?limit=100" : null);
  const [senderId, setSenderId] = React.useState("");
  const [to, setTo] = React.useState("");
  const [vars, setVars] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const usable = (senders.data?.items ?? []).filter((s) => senderBlockReason(s) === null);
  React.useEffect(() => {
    if (open) {
      setTo("");
      setVars({});
      setError(null);
    }
  }, [open]);
  React.useEffect(() => {
    if (!senderId && usable[0]) setSenderId(usable[0].id);
  }, [usable, senderId]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ message_id: string; state: string }>(`/api/v1/email/templates/${templateId}/test-send`, {
        method: "POST",
        body: { sender_id: senderId, to: to.trim(), version, variables: cleanVars(vars) },
        idempotencyKey: key || true,
      });
      toast({ tone: "success", title: "Test message queued", description: `State: ${out.state}. It goes through the normal suppression and quota checks.` });
      onClose();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} busy={busy} size="lg" title={`Send a test of version ${version}`} description="One rendered copy, sent as a transactional message. Test sends are limited per hour.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="ts-sender" label="From" required error={error?.fields.sender_id}>
            {(p) => (
              <Select {...p} value={senderId} onChange={(e) => setSenderId(e.target.value)} disabled={senders.loading}>
                {usable.length === 0 ? <option value="">{senders.loading ? "Loading…" : "No verified sender"}</option> : null}
                {(senders.data?.items ?? []).map((s) => {
                  const block = senderBlockReason(s);
                  return (
                    <option key={s.id} value={s.id} disabled={block !== null}>
                      {s.from_name} &lt;{s.from_address}&gt;{block ? ` — ${block}` : ""}
                    </option>
                  );
                })}
              </Select>
            )}
          </Field>
          <Field id="ts-to" label="To" required error={error?.fields.to}>
            {(p) => <Input {...p} type="email" value={to} maxLength={320} onChange={(e) => setTo(e.target.value)} autoComplete="email" />}
          </Field>
        </div>
        <VariablesInputs values={vars} onChange={setVars} />
        <FormError message={error && !error.isStepUp && Object.keys(error.fields).length === 0 ? (error.status === 429 ? "The hourly test-send limit is reached. Try again later." : error.message) : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !senderId || !to.trim()}>
            {busy ? "Sending…" : "Send test"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
