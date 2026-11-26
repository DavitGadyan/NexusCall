"use client";
/**
 * CSV imports: upload (≤ 10 MB) with a column preview and import options (default region, source, optional
 * consent evidence), the list of imports with live progress, and the result page with the per-row report.
 */
import { Download, Upload } from "lucide-react";
import Link from "next/link";
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
  Stat,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { JobBadge, MutationError } from "./common";
import { buildImportOptions, type CsvPreview, PREVIEW_BYTES, previewCsv, STANDARD_COLUMNS } from "./lib/csv-preview";
import { formatBytes, newIdempotencyKey, openDownload, uploadFile, validateUploadFile } from "./lib/http";
import { type FieldDef, type ImportJob, type Items, TERMINAL_JOB } from "./lib/types";
import { usePoll } from "./lib/use-poll";

export function importProgress(job: Pick<ImportJob, "total_rows" | "created_count" | "updated_count" | "failed_count" | "status">): number | null {
  if (job.status === "completed") return 100;
  if (!job.total_rows) return null;
  const done = job.created_count + job.updated_count + job.failed_count;
  return Math.max(0, Math.min(100, Math.round((done / job.total_rows) * 100)));
}

function Progress({ job }: { job: ImportJob }) {
  const pct = importProgress(job);
  if (job.status === "failed") return <JobBadge status="failed" />;
  return (
    <div className="flex min-w-[8rem] items-center gap-2">
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-2" role="progressbar" aria-label="Import progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct ?? undefined}>
        <div className={pct === null ? "h-full w-1/3 animate-pulse bg-brand/60" : "h-full bg-brand"} style={pct === null ? undefined : { width: `${pct}%` }} />
      </div>
      <span className="tabular w-10 text-right text-xs text-muted">{pct === null ? "…" : `${pct}%`}</span>
    </div>
  );
}

export function Imports() {
  const can = useCan();
  const router = useRouter();
  const list = useCursorList<ImportJob>("/api/v1/imports");
  const [open, setOpen] = React.useState(false);
  usePoll(list.reload, list.items.some((i) => !TERMINAL_JOB.has(i.status)));

  const columns: Column<ImportJob>[] = [
    { key: "file", header: "File", cell: (i) => <Link href={`/app/crm/imports/${i.id}`} className="font-medium text-brand hover:underline">{i.filename}</Link> },
    { key: "status", header: "Status", cell: (i) => <JobBadge status={i.status} /> },
    { key: "progress", header: "Progress", cell: (i) => <Progress job={i} />, hideOnMobile: true },
    { key: "rows", header: "Created / updated / failed", cell: (i) => <span className="tabular">{i.created_count} / {i.updated_count} / <span className={i.failed_count ? "text-danger" : undefined}>{i.failed_count}</span></span> },
    { key: "size", header: "Size", cell: (i) => formatBytes(i.size_bytes), hideOnMobile: true },
    { key: "created", header: "Uploaded", cell: (i) => <span title={formatDateTime(i.created_at)}>{relativeTime(i.created_at)}</span>, hideOnMobile: true },
  ];

  return (
    <Card>
      <CardHeader
        title="Imports"
        description="CSV files of contacts. Existing contacts (same email or phone) are updated; each row's result is in the report."
        actions={can("leads:create") ? <Button size="sm" onClick={() => setOpen(true)} data-testid="new-import"><Upload className="h-4 w-4" aria-hidden /> Import CSV</Button> : null}
      />
      <DataTable
        caption="Imports"
        columns={columns}
        rows={list.items}
        rowKey={(i) => i.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={<EmptyState title="No imports yet" description="Upload a CSV file with an email or phone column." />}
      />
      <ImportDialog open={open} onClose={() => setOpen(false)} onStarted={(job) => router.push(`/app/crm/imports/${job.id}`)} />
    </Card>
  );
}

/** `Unknown CSV columns` problems carry the offending names in `columns` (crm_service imports.check_header). */
function unknownColumns(error: ApiError | null): string[] {
  const cols = error?.problem.columns;
  return Array.isArray(cols) ? cols.filter((c): c is string => typeof c === "string").slice(0, 20) : [];
}

const BASES = ["consent", "legitimate_interest", "contract", "legal_obligation", "vital_interest", "public_task"];

function ImportDialog({ open, onClose, onStarted }: { open: boolean; onClose: () => void; onStarted: (job: ImportJob) => void }) {
  const toast = useToast();
  const fields = useApi<Items<FieldDef>>(open ? "/api/v1/custom-fields" : null);
  const [file, setFile] = React.useState<File | null>(null);
  const [fileError, setFileError] = React.useState<string | null>(null);
  const [preview, setPreview] = React.useState<CsvPreview | null>(null);
  const [opts, setOpts] = React.useState({ region: "US", source: "import", consentChannels: [] as ("phone" | "email")[], consentStatus: "granted" as "granted" | "withdrawn", lawfulBasis: "consent", consentSource: "", policyVersion: "", evidenceRef: "" });
  const [optErrors, setOptErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const key = React.useRef("");
  const customKeys = React.useMemo(() => new Set((fields.data?.items ?? []).map((f) => f.key)), [fields.data]);
  const fileText = React.useRef<string>("");

  React.useEffect(() => {
    if (!open) return;
    setFile(null);
    setFileError(null);
    setPreview(null);
    setOptErrors({});
    setError(null);
    fileText.current = "";
  }, [open]);

  React.useEffect(() => {
    if (fileText.current) setPreview(previewCsv(fileText.current, customKeys));
  }, [customKeys]);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null;
    setError(null);
    setPreview(null);
    setFile(null);
    if (!f) return;
    const problem = validateUploadFile(f, { extensions: [".csv"] });
    setFileError(problem);
    if (problem) return;
    setFile(f);
    key.current = newIdempotencyKey(); // one key per chosen file: a retry never imports twice
    fileText.current = await f.slice(0, PREVIEW_BYTES).text();
    setPreview(previewCsv(fileText.current, customKeys));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    const built = buildImportOptions(opts);
    setOptErrors(built.errors);
    if (!built.options) return;
    setBusy(true);
    setError(null);
    try {
      const job = await uploadFile<ImportJob>("/api/v1/imports", file, { fields: { options: built.options }, idempotencyKey: key.current });
      toast({ tone: "success", title: "Import started", description: file.name });
      onStarted(job);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const blocking = preview?.problems.length ? preview.problems : [];
  const setChannel = (ch: "phone" | "email", on: boolean) => setOpts((o) => ({ ...o, consentChannels: on ? [...new Set([...o.consentChannels, ch])] : o.consentChannels.filter((c) => c !== ch) }));

  return (
    <Dialog open={open} onClose={onClose} busy={busy} size="xl" title="Import contacts from CSV" description={`UTF-8 CSV, at most 10 MB. Columns: ${STANDARD_COLUMNS.join(", ")}, or custom.<key> for custom fields.`}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-3">
          <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-2 focus-within:ring-2 focus-within:ring-brand">
            <Upload className="h-4 w-4" aria-hidden /> Choose CSV file
            <input type="file" accept=".csv,text/csv" className="sr-only" onChange={onFile} data-testid="import-file" />
          </label>
          {file ? <span className="text-sm">{file.name} <span className="text-muted">({formatBytes(file.size)})</span></span> : <span className="text-xs text-muted">No file chosen</span>}
        </div>
        <FormError message={fileError} />
        {preview ? (
          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium">Column preview</p>
            <ul className="flex flex-wrap gap-1.5" aria-label="Columns">
              {preview.columns.map((c, i) => (
                <li key={`${c.name}-${i}`}>
                  <Badge tone={c.status === "standard" ? "ok" : c.status === "custom" ? "brand" : "danger"}>{c.name || "(empty)"}{c.status === "unknown" ? " · unknown" : c.status === "duplicate" ? " · duplicate" : ""}</Badge>
                </li>
              ))}
            </ul>
            {preview.rows.length > 0 ? (
              <div className="max-h-56 overflow-auto rounded-md border border-border">
                <table className="w-full text-left text-xs">
                  <caption className="sr-only">First rows of the file</caption>
                  <thead>
                    <tr className="text-muted">{preview.columns.map((c, i) => <th key={i} scope="col" className="whitespace-nowrap px-3 py-1.5">{c.name}</th>)}</tr>
                  </thead>
                  <tbody>
                    {preview.rows.map((r, i) => (
                      <tr key={i} className="border-t border-border">
                        {preview.columns.map((_, j) => <td key={j} className="max-w-[12rem] truncate px-3 py-1.5">{r[j] ?? ""}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            {blocking.map((m) => <FormError key={m} message={m} />)}
          </div>
        ) : null}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="import-region" label="Default country for phone numbers" hint="Used when a number has no country code." error={optErrors.region}>
            {(p) => <Input {...p} maxLength={2} value={opts.region} onChange={(e) => setOpts((o) => ({ ...o, region: e.target.value.toUpperCase() }))} />}
          </Field>
          <Field id="import-source" label="Source for new contacts" error={optErrors.source}>
            {(p) => <Input {...p} maxLength={64} value={opts.source} onChange={(e) => setOpts((o) => ({ ...o, source: e.target.value }))} />}
          </Field>
        </div>
        <fieldset className="flex flex-col gap-3 rounded-md border border-border p-4">
          <legend className="px-1 text-sm font-medium">Consent evidence (optional)</legend>
          <p className="text-xs text-muted">Only record consent you can prove. It is stored per contact with your identity as the recorder.</p>
          <div className="flex gap-4">
            <Checkbox label="Phone" checked={opts.consentChannels.includes("phone")} onChange={(e) => setChannel("phone", e.target.checked)} />
            <Checkbox label="Email" checked={opts.consentChannels.includes("email")} onChange={(e) => setChannel("email", e.target.checked)} />
          </div>
          {opts.consentChannels.length > 0 ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Field id="import-consent-status" label="Status">
                {(p) => <Select {...p} value={opts.consentStatus} onChange={(e) => setOpts((o) => ({ ...o, consentStatus: e.target.value as "granted" | "withdrawn" }))}><option value="granted">Granted</option><option value="withdrawn">Withdrawn</option></Select>}
              </Field>
              <Field id="import-consent-basis" label="Lawful basis">
                {(p) => <Select {...p} value={opts.lawfulBasis} onChange={(e) => setOpts((o) => ({ ...o, lawfulBasis: e.target.value }))}>{BASES.map((b) => <option key={b} value={b}>{humanize(b)}</option>)}</Select>}
              </Field>
              <Field id="import-consent-source" label="Where it was captured" required error={optErrors.consentSource}>
                {(p) => <Input {...p} maxLength={64} value={opts.consentSource} onChange={(e) => setOpts((o) => ({ ...o, consentSource: e.target.value }))} />}
              </Field>
              <Field id="import-consent-policy" label="Policy version">
                {(p) => <Input {...p} maxLength={32} value={opts.policyVersion} onChange={(e) => setOpts((o) => ({ ...o, policyVersion: e.target.value }))} />}
              </Field>
              <Field id="import-consent-evidence" label="Evidence reference" className="sm:col-span-2">
                {(p) => <Input {...p} maxLength={255} value={opts.evidenceRef} onChange={(e) => setOpts((o) => ({ ...o, evidenceRef: e.target.value }))} />}
              </Field>
            </div>
          ) : null}
        </fieldset>
        <FormError message={optErrors._form} />
        <MutationError error={error} />
        {unknownColumns(error).length ? <Notice tone="danger" title="Unknown columns">{unknownColumns(error).join(", ")}</Notice> : null}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy || !file || blocking.length > 0} data-testid="start-import">{busy ? "Uploading…" : "Start import"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

export function ImportDetail({ importId }: { importId: string }) {
  const toast = useToast();
  const job = useApi<ImportJob>(`/api/v1/imports/${importId}`);
  const [busy, setBusy] = React.useState(false);
  usePoll(job.reload, Boolean(job.data && !TERMINAL_JOB.has(job.data.status)));

  if (job.loading && !job.data) return <Card><LoadingState /></Card>;
  if (job.error && !job.data) return <Card><ErrorState problem={job.error.problem} onRetry={job.reload} /></Card>;
  const j = job.data;
  if (!j) return null;
  const running = !TERMINAL_JOB.has(j.status);

  async function report() {
    setBusy(true);
    try {
      const link = await api<{ url: string; expires_in: number }>(`/api/v1/imports/${importId}/report`);
      if (!openDownload(link.url)) toast({ tone: "error", title: "The report link was not valid" });
    } catch (err) {
      const e = toApiError(err);
      if (!e.isStepUp) toast({ tone: "error", title: "Could not get the report", description: e.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title={j.filename}
        description={`Uploaded ${formatDateTime(j.created_at)} · ${formatBytes(j.size_bytes)}`}
        actions={j.has_report ? <Button variant="secondary" onClick={report} disabled={busy} data-testid="download-report"><Download className="h-4 w-4" aria-hidden /> {busy ? "Preparing…" : "Download row report"}</Button> : null}
      />
      <div className="flex flex-col gap-4">
        {running ? (
          <Card className="px-5 py-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className="inline-flex items-center gap-2 text-sm"><JobBadge status={j.status} /> Importing… this page updates automatically.</span>
              <div className="w-full sm:w-72"><Progress job={j} /></div>
            </div>
          </Card>
        ) : j.status === "failed" ? (
          <Notice tone="danger" title="The import failed">{j.error_code ? humanize(j.error_code) : "No rows were imported."}</Notice>
        ) : (
          <Notice tone={j.failed_count ? "warn" : "ok"} title="Import finished">
            {j.failed_count ? `${j.failed_count} row${j.failed_count === 1 ? "" : "s"} could not be imported. The row report lists the reason for each.` : "Every row was imported."}
          </Notice>
        )}
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Rows" value={j.total_rows} />
          <Stat label="Created" value={j.created_count} />
          <Stat label="Updated" value={j.updated_count} />
          <Stat label="Failed" value={j.failed_count} />
        </div>
        <Card>
          <CardHeader title="Details" />
          <div className="px-5 py-4">
            <KeyValues items={[
              { label: "Status", value: <JobBadge status={j.status} /> },
              { label: "Started", value: formatDateTime(j.created_at) },
              { label: "Finished", value: formatDateTime(j.completed_at) },
              { label: "Row report", value: j.has_report ? "Available (link valid for a few minutes, audited)" : running ? "When the import finishes" : "None" },
            ]} />
          </div>
        </Card>
        <Link href="/app/crm/imports" className="text-sm text-brand hover:underline">All imports</Link>
      </div>
    </>
  );
}
