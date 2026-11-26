"use client";
/**
 * Knowledge source: settings (classification can be tightened; approval is recomputed server-side),
 * document upload (txt, md, pdf ≤ 10 MB) with ingestion status, human review (step-up) and deletion.
 */
import { Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { formatDateTime, humanize, relativeTime } from "@/lib/format";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  type Column,
  ConfirmDialog,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  FormError,
  Input,
  KeyValues,
  LoadingState,
  Notice,
  PageHeader,
  Select,
  StatusBadge,
  Tabs,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { JobBadge, MutationError, StepUpHint } from "@/components/crm/common";
import { formatBytes, newIdempotencyKey, uploadFile, validateUploadFile } from "@/components/crm/lib/http";
import { usePoll } from "@/components/crm/lib/use-poll";
import { CLASSIFICATIONS, type KnowledgeDocument, type KnowledgeSource } from "../lib/types";
import { approvalRequired, ClassificationBadge } from "./Knowledge";

const AWAITING = new Set(["pending_approval", "needs_review"]);
export const DOC_EXTENSIONS = [".txt", ".md", ".pdf"] as const;

export function reviewTone(state: string): "neutral" | "ok" | "warn" | "danger" | "brand" {
  if (state === "approved" || state === "not_required") return "ok";
  if (AWAITING.has(state)) return "warn";
  if (state === "rejected") return "danger";
  return "neutral";
}

/** Whether agents can retrieve the document now (indexed and either approved or not needing review). */
export function isRetrievable(d: Pick<KnowledgeDocument, "status" | "review_state">): boolean {
  return d.status === "indexed" && (d.review_state === "approved" || d.review_state === "not_required");
}

export function SourceDetail({ sourceId }: { sourceId: string }) {
  const source = useApi<KnowledgeSource>(`/api/v1/ai/knowledge/sources/${sourceId}`);
  const [tab, setTab] = React.useState("documents");
  if (source.loading && !source.data) return <Card><LoadingState /></Card>;
  if (source.error && !source.data) return <Card><ErrorState problem={source.error.problem} onRetry={source.reload} /></Card>;
  const s = source.data;
  if (!s) return null;
  return (
    <>
      <PageHeader title={s.name} description={s.description || undefined} />
      <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
        <ClassificationBadge value={s.classification} />
        <Badge>{s.origin}</Badge>
        <StatusBadge status={s.status} />
        {s.requires_approval ? <Badge tone="warn">Every document reviewed</Badge> : null}
      </div>
      <Tabs label="Source sections" value={tab} onChange={setTab} tabs={[{ id: "documents", label: "Documents" }, { id: "settings", label: "Settings" }]}>
        {tab === "documents" ? <Documents source={s} /> : <SourceSettings source={s} onSaved={(next) => source.setData(() => next)} onReload={source.reload} />}
      </Tabs>
    </>
  );
}

function Documents({ source }: { source: KnowledgeSource }) {
  const can = useCan();
  const toast = useToast();
  const list = useCursorList<KnowledgeDocument>(`/api/v1/ai/knowledge/sources/${source.id}/documents`);
  const [uploading, setUploading] = React.useState(false);
  const [reviewing, setReviewing] = React.useState<KnowledgeDocument | null>(null);
  const [deleting, setDeleting] = React.useState<KnowledgeDocument | null>(null);
  const manage = can("ai:agents:manage");
  usePoll(list.reload, list.items.some((d) => d.status === "queued" || d.status === "processing" || d.review_state === "pending_analysis"));

  const columns: Column<KnowledgeDocument>[] = [
    { key: "file", header: "Document", cell: (d) => <span className="min-w-0"><span className="block truncate font-medium">{d.filename}</span><span className="block text-xs text-muted">{formatBytes(d.size_bytes)} · v{d.doc_version}</span></span> },
    { key: "status", header: "Ingestion", cell: (d) => <span className="flex flex-col gap-0.5"><JobBadge status={d.status} />{d.error_code ? <span className="text-xs text-danger">{humanize(d.error_code)}</span> : null}</span> },
    { key: "scan", header: "Scan", cell: (d) => <Badge tone={d.scan_status === "suspicious" ? "danger" : d.scan_status === "clean" ? "ok" : "neutral"}>{d.scan_status}</Badge>, hideOnMobile: true },
    {
      key: "review",
      header: "Review",
      cell: (d) => (
        <span className="flex flex-col gap-0.5">
          <Badge tone={reviewTone(d.review_state)}>{humanize(d.review_state)}</Badge>
          {d.review_reasons.length ? <span className="text-xs text-muted">{d.review_reasons.map(humanize).join(", ")}</span> : null}
          {d.pii_detected ? <span className="text-xs text-warn">Personal data detected</span> : null}
        </span>
      ),
    },
    { key: "chunks", header: "Chunks", cell: (d) => <span className="tabular">{d.chunk_count}</span>, hideOnMobile: true },
    { key: "live", header: "Retrievable", cell: (d) => (isRetrievable(d) ? <Badge tone="ok">yes</Badge> : <span className="text-xs text-muted">no</span>), hideOnMobile: true },
    { key: "at", header: "Uploaded", cell: (d) => <span title={formatDateTime(d.created_at)}>{relativeTime(d.created_at)}</span>, hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (d) =>
        manage ? (
          <div className="flex justify-end gap-1">
            {d.status === "indexed" && AWAITING.has(d.review_state) ? <Button size="sm" onClick={() => setReviewing(d)} data-testid="review-document">Review</Button> : null}
            <Button size="sm" variant="ghost" onClick={() => setDeleting(d)}>Delete</Button>
          </div>
        ) : null,
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Documents"
        description="Uploads are scanned, extracted and checked for personal data and injected instructions. Flagged documents are not retrievable until a person approves them."
        actions={manage && source.status === "active" ? <Button size="sm" onClick={() => setUploading(true)} data-testid="upload-document"><Upload className="h-4 w-4" aria-hidden /> Upload</Button> : null}
      />
      {source.status !== "active" ? <div className="px-5 pt-4"><Notice tone="warn">This source is disabled: uploads are refused and agents do not retrieve from it.</Notice></div> : null}
      <DataTable caption="Documents" columns={columns} rows={list.items} rowKey={(d) => d.id} loading={list.loading} error={list.error} onRetry={list.reload} hasMore={list.hasMore} loadingMore={list.loadingMore} onLoadMore={list.loadMore} empty={<EmptyState title="No documents yet" description="Upload .txt, .md or .pdf files up to 10 MB." />} />
      <UploadDialog open={uploading} sourceId={source.id} onClose={() => setUploading(false)} onUploaded={(d) => { setUploading(false); list.reload(); toast({ tone: "success", title: "Upload accepted", description: `${d.filename} is being processed.` }); }} />
      <ReviewDialog doc={reviewing} onClose={() => setReviewing(null)} onDone={(d) => { setReviewing(null); list.reload(); toast({ tone: d.review_state === "approved" ? "success" : "info", title: d.review_state === "approved" ? "Document approved" : "Document rejected" }); }} />
      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title="Delete document"
        description={`"${deleting?.filename ?? ""}", its chunks and embeddings are removed. Agents stop retrieving it immediately.`}
        confirmLabel="Delete"
        onConfirm={async () => {
          if (!deleting) return;
          await api(`/api/v1/ai/knowledge/documents/${deleting.id}`, { method: "DELETE" });
          list.reload();
          toast({ tone: "success", title: "Document deleted" });
        }}
      />
    </Card>
  );
}

function UploadDialog({ open, sourceId, onClose, onUploaded }: { open: boolean; sourceId: string; onClose: () => void; onUploaded: (d: KnowledgeDocument) => void }) {
  const [file, setFile] = React.useState<File | null>(null);
  const [fileError, setFileError] = React.useState<string | null>(null);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef("");
  React.useEffect(() => {
    if (open) {
      setFile(null);
      setFileError(null);
      setError(null);
    }
  }, [open]);
  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null;
    setError(null);
    if (!f) return setFile(null);
    const problem = validateUploadFile(f, { extensions: DOC_EXTENSIONS });
    setFileError(problem);
    setFile(problem ? null : f);
    key.current = newIdempotencyKey();
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      onUploaded(await uploadFile<KnowledgeDocument>(`/api/v1/ai/knowledge/sources/${sourceId}/documents`, file, { idempotencyKey: key.current }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  const code = typeof error?.problem.code_detail === "string" ? error.problem.code_detail : null;
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Upload a document" description="Text (.txt), Markdown (.md) or PDF, at most 10 MB. The type is checked from the content too.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <label className="inline-flex w-fit cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-2 focus-within:ring-2 focus-within:ring-brand">
          <Upload className="h-4 w-4" aria-hidden /> Choose file
          <input type="file" accept=".txt,.md,.pdf,text/plain,text/markdown,application/pdf" className="sr-only" onChange={onFile} data-testid="document-file" />
        </label>
        {file ? <p className="text-sm">{file.name} <span className="text-muted">({formatBytes(file.size)})</span></p> : null}
        <FormError message={fileError} />
        {code ? <FormError message={`${error?.message ?? "Rejected"}: ${humanize(code)}`} /> : <MutationError error={error} />}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy || !file}>{busy ? "Uploading…" : "Upload"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function ReviewDialog({ doc, onClose, onDone }: { doc: KnowledgeDocument | null; onClose: () => void; onDone: (d: KnowledgeDocument) => void }) {
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState<"approve" | "reject" | null>(null);
  React.useEffect(() => {
    if (doc) {
      setNote("");
      setError(null);
    }
  }, [doc]);
  async function decide(decision: "approve" | "reject") {
    if (!doc) return;
    setBusy(decision);
    setError(null);
    try {
      onDone(await api<KnowledgeDocument>(`/api/v1/ai/knowledge/documents/${doc.id}/review`, { method: "POST", body: { decision, note: note.trim() } }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(null);
    }
  }
  return (
    <Dialog open={doc !== null} onClose={onClose} busy={busy !== null} size="lg" title="Review document" description="Approving makes the document retrievable by agents. Rejecting removes its chunks. The decision is audited.">
      {doc ? (
        <div className="flex flex-col gap-4">
          <KeyValues items={[
            { label: "File", value: doc.filename },
            { label: "Reasons", value: doc.review_reasons.length ? doc.review_reasons.map(humanize).join(", ") : "Source requires review" },
            { label: "Scan", value: doc.scan_status },
            { label: "Personal data", value: doc.pii_detected ? "Detected" : "Not detected" },
            { label: "Chunks", value: doc.chunk_count },
            { label: "SHA-256", value: <code className="break-all text-xs">{doc.sha256}</code> },
            { label: "Uploaded by", value: doc.uploaded_by ?? "—" },
          ]} />
          <Notice tone="info"><span className="inline-flex flex-wrap items-center gap-2"><StepUpHint /> Reviewing needs a recent multi-factor sign-in.</span></Notice>
          <Field id="review-note" label="Note (optional)" error={error?.fields.note}>
            {(p) => <Textarea {...p} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />}
          </Field>
          <MutationError error={error} />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={onClose} disabled={busy !== null}>Cancel</Button>
            <Button variant="danger" onClick={() => decide("reject")} disabled={busy !== null}>{busy === "reject" ? "Rejecting…" : "Reject"}</Button>
            <Button onClick={() => decide("approve")} disabled={busy !== null} data-testid="approve-document">{busy === "approve" ? "Approving…" : "Approve"}</Button>
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}

function SourceSettings({ source, onSaved, onReload }: { source: KnowledgeSource; onSaved: (s: KnowledgeSource) => void; onReload: () => void }) {
  const can = useCan();
  const router = useRouter();
  const toast = useToast();
  const [f, setF] = React.useState({ name: source.name, description: source.description, classification: source.classification, requires_approval: source.requires_approval, status: source.status, roles: source.allowed_roles.join(", ") });
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  React.useEffect(() => {
    setF({ name: source.name, description: source.description, classification: source.classification, requires_approval: source.requires_approval, status: source.status, roles: source.allowed_roles.join(", ") });
  }, [source]);
  const manage = can("ai:agents:manage");
  const forced = approvalRequired(source.origin, f.classification, false);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const roles = f.roles.split(",").map((r) => r.trim()).filter(Boolean);
    const body: Record<string, unknown> = { version: source.version };
    if (f.name.trim() !== source.name) body.name = f.name.trim();
    if (f.description.trim() !== source.description) body.description = f.description.trim();
    if (f.classification !== source.classification) body.classification = f.classification;
    if ((f.requires_approval || forced) !== source.requires_approval) body.requires_approval = f.requires_approval || forced;
    if (f.status !== source.status) body.status = f.status;
    if (roles.join(",") !== source.allowed_roles.join(",")) body.allowed_roles = roles;
    try {
      onSaved(await api<KnowledgeSource>(`/api/v1/ai/knowledge/sources/${source.id}`, { method: "PATCH", body }));
      toast({ tone: "success", title: "Source saved" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Card>
      <CardHeader title="Settings" />
      <form onSubmit={save} className="flex flex-col gap-4 px-5 py-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="ss-name" label="Name" error={error?.fields.name}>
            {(p) => <Input {...p} disabled={!manage} minLength={2} maxLength={120} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />}
          </Field>
          <Field id="ss-class" label="Classification" hint="Raising it to confidential or restricted puts existing documents back into review." error={error?.fields.classification}>
            {(p) => <Select {...p} disabled={!manage} value={f.classification} onChange={(e) => setF({ ...f, classification: e.target.value })}>{CLASSIFICATIONS.map((c) => <option key={c} value={c}>{c}</option>)}</Select>}
          </Field>
          <Field id="ss-status" label="Status" error={error?.fields.status}>
            {(p) => <Select {...p} disabled={!manage} value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}><option value="active">Active</option><option value="disabled">Disabled</option></Select>}
          </Field>
          <Field id="ss-roles" label="Restrict to roles" hint="Comma-separated role keys; empty = all roles." error={error?.fields.allowed_roles}>
            {(p) => <Input {...p} disabled={!manage} value={f.roles} onChange={(e) => setF({ ...f, roles: e.target.value })} />}
          </Field>
        </div>
        <Field id="ss-desc" label="Description" error={error?.fields.description}>
          {(p) => <Textarea {...p} disabled={!manage} maxLength={500} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} />}
        </Field>
        <Checkbox label="Every document needs human approval" checked={f.requires_approval || forced} disabled={!manage || forced} description={forced ? "Always on for external, confidential and restricted sources." : undefined} onChange={(e) => setF({ ...f, requires_approval: e.target.checked })} />
        <KeyValues items={[{ label: "Origin", value: source.origin }, { label: "Created", value: formatDateTime(source.created_at) }]} />
        <MutationError error={error} onReload={() => { setError(null); onReload(); }} />
        {manage ? (
          <div className="flex justify-between gap-2">
            <Button type="button" variant="ghost" onClick={() => setDeleting(true)}>Delete source</Button>
            <Button type="submit" disabled={busy}>{busy ? "Saving…" : "Save"}</Button>
          </div>
        ) : null}
      </form>
      <ConfirmDialog
        open={deleting}
        onClose={() => setDeleting(false)}
        title="Delete knowledge source"
        description="The source, all its documents, chunks and embeddings are deleted in one step, and the stored files are removed. Agents referencing it lose access."
        confirmLabel="Delete source"
        typedConfirmation={source.name}
        onConfirm={async () => {
          await api(`/api/v1/ai/knowledge/sources/${source.id}`, { method: "DELETE" });
          toast({ tone: "success", title: "Source deleted" });
          router.push("/app/knowledge");
        }}
      />
    </Card>
  );
}
