"use client";
/** Segments: list, and the rule builder (allowlisted fields and operators, nested AND/OR, live preview count). */
import { Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import {
  Button,
  ButtonLink,
  Card,
  CardHeader,
  type Column,
  ConfirmDialog,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  Input,
  LoadingState,
  PageHeader,
  Select,
  Spinner,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { MutationError } from "./common";
import {
  countRules,
  describe,
  emptyGroup,
  fieldCatalogue,
  type FieldSpec,
  fromDsl,
  type GroupNode,
  LIST_OPS,
  MAX_DEPTH,
  MAX_RULES,
  NO_VALUE,
  type Node,
  newRule,
  OPERATOR_LABEL,
  reducer,
  type RuleNode,
  toDsl,
} from "./lib/segment-rules";
import type { FieldDef, Items, Segment, SegmentPreview } from "./lib/types";

export function Segments() {
  const can = useCan();
  const list = useApi<Items<Segment>>("/api/v1/segments");
  const fields = useApi<Items<FieldDef>>("/api/v1/custom-fields");
  const catalogue = React.useMemo(() => fieldCatalogue(fields.data?.items ?? []), [fields.data]);
  const columns: Column<Segment>[] = [
    { key: "name", header: "Segment", cell: (s) => <Link className="font-medium text-brand hover:underline" href={`/app/crm/segments/${s.id}`}>{s.name}</Link> },
    { key: "rules", header: "Rules", cell: (s) => <span className="line-clamp-2 text-muted">{describe(s.rules, catalogue)}</span> },
    { key: "updated", header: "Updated", cell: (s) => <span title={formatDateTime(s.updated_at)}>{relativeTime(s.updated_at)}</span>, hideOnMobile: true },
  ];
  return (
    <Card>
      <CardHeader
        title="Segments"
        description="Saved audiences for campaigns. Membership is evaluated when a campaign launches."
        actions={can("campaigns:create") ? <ButtonLink size="sm" variant="primary" href="/app/crm/segments/new" data-testid="new-segment">New segment</ButtonLink> : null}
      />
      <DataTable caption="Segments" columns={columns} rows={list.data?.items ?? []} rowKey={(s) => s.id} loading={list.loading} error={list.error} onRetry={list.reload} empty={<EmptyState title="No segments yet" description="Build an audience from contact fields, tags and lead status." />} />
    </Card>
  );
}

export function SegmentEditor({ segmentId }: { segmentId: string | null }) {
  const can = useCan();
  const router = useRouter();
  const toast = useToast();
  const existing = useApi<Segment>(segmentId ? `/api/v1/segments/${segmentId}` : null);
  const fields = useApi<Items<FieldDef>>("/api/v1/custom-fields");
  const catalogue = React.useMemo(() => fieldCatalogue(fields.data?.items ?? []), [fields.data]);
  const [root, dispatch] = React.useReducer(reducer, undefined, () => ({ ...emptyGroup("and"), children: [newRule()] }));
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [channel, setChannel] = React.useState("phone");
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const loadedVersion = React.useRef<number | null>(null);

  React.useEffect(() => {
    const s = existing.data;
    if (!s || loadedVersion.current === s.version) return;
    loadedVersion.current = s.version;
    setName(s.name);
    setDescription(s.description ?? "");
    dispatch({ type: "replace", root: fromDsl(s.rules) });
  }, [existing.data]);

  const compiled = React.useMemo(() => toDsl(root, catalogue), [root, catalogue]);
  const preview = usePreview(compiled.dsl, channel);
  const canSave = can("campaigns:create");

  if (segmentId && existing.loading && !existing.data) return <Card><LoadingState /></Card>;
  if (segmentId && existing.error && !existing.data) return <Card><ErrorState problem={existing.error.problem} onRetry={existing.reload} /></Card>;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!compiled.dsl) return;
    setBusy(true);
    setError(null);
    try {
      const body = { name: name.trim(), description: description.trim() || null, rules: compiled.dsl };
      const saved = segmentId && existing.data
        ? await api<Segment>(`/api/v1/segments/${segmentId}`, { method: "PATCH", body: { ...body, expected_version: existing.data.version } })
        : await api<Segment>("/api/v1/segments", { method: "POST", body });
      toast({ tone: "success", title: "Segment saved" });
      if (segmentId) existing.setData(() => saved);
      else router.push(`/app/crm/segments/${saved.id}`);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title={segmentId ? existing.data?.name ?? "Segment" : "New segment"}
        description="Rules use an allowlist of contact fields and operators. Suppressed and non-consenting contacts are never called, whatever the segment says."
        actions={segmentId && can("campaigns:manage") ? <Button variant="ghost" onClick={() => setDeleting(true)}>Delete</Button> : null}
      />
      <form onSubmit={save} className="grid gap-4 lg:grid-cols-3">
        <div className="flex flex-col gap-4 lg:col-span-2">
          <Card className="px-5 py-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field id="seg-name" label="Name" required error={error?.fields.name}>
                {(p) => <Input {...p} required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} disabled={!canSave} />}
              </Field>
              <Field id="seg-desc" label="Description" error={error?.fields.description}>
                {(p) => <Textarea {...p} className="min-h-[36px]" rows={1} maxLength={1000} value={description} onChange={(e) => setDescription(e.target.value)} disabled={!canSave} />}
              </Field>
            </div>
          </Card>
          <Card>
            <CardHeader title="Rules" description={`Up to ${MAX_RULES} rules, nested ${MAX_DEPTH} levels deep. List values are comma separated.`} />
            <div className="px-4 py-4">
              <GroupEditor group={root} depth={1} catalogue={catalogue} errors={compiled.errors} dispatch={dispatch} disabled={!canSave} isRoot />
            </div>
          </Card>
          <MutationError error={error} onReload={() => { setError(null); loadedVersion.current = null; existing.reload(); }} />
          {error?.fields.rules ? <p className="text-sm text-danger">{error.fields.rules}</p> : null}
          {canSave ? (
            <div className="flex justify-end gap-2">
              <ButtonLink href="/app/crm/segments">Cancel</ButtonLink>
              <Button type="submit" disabled={busy || !compiled.dsl || !name.trim()} data-testid="save-segment">{busy ? "Saving…" : "Save segment"}</Button>
            </div>
          ) : null}
        </div>
        <Card className="h-fit lg:sticky lg:top-4">
          <CardHeader title="Preview" description="Live count of matching contacts." />
          <div className="flex flex-col gap-3 px-5 py-4">
            <Field id="seg-channel" label="Contactable on">
              {(p) => <Select {...p} value={channel} onChange={(e) => setChannel(e.target.value)}><option value="phone">Phone</option><option value="email">Email</option><option value="">Any (no contactability check)</option></Select>}
            </Field>
            <div aria-live="polite" className="flex flex-col gap-1">
              {!compiled.dsl ? <p className="text-sm text-muted">Fix the highlighted rules to see a count.</p> : preview.error ? <p className="text-sm text-danger">{preview.error.message}</p> : preview.data ? (
                <>
                  <p className="text-sm"><span className="tabular text-2xl font-semibold">{preview.data.matching.toLocaleString()}</span> matching</p>
                  {preview.data.contactable !== null ? <p className="text-sm text-muted"><span className="tabular font-medium text-fg">{preview.data.contactable.toLocaleString()}</span> contactable by {preview.data.channel}</p> : null}
                </>
              ) : null}
              {preview.loading ? <Spinner label="Updating preview" /> : null}
            </div>
            <p className="text-xs text-muted">{describe(compiled.dsl, catalogue)}</p>
          </div>
        </Card>
      </form>
      <ConfirmDialog
        open={deleting}
        onClose={() => setDeleting(false)}
        title="Delete segment"
        description="Campaigns that already launched keep their audience snapshot."
        confirmLabel="Delete"
        onConfirm={async () => {
          await api(`/api/v1/segments/${segmentId}`, { method: "DELETE" });
          toast({ tone: "success", title: "Segment deleted" });
          router.push("/app/crm/segments");
        }}
      />
    </>
  );
}

function usePreview(dsl: unknown, channel: string) {
  const [state, setState] = React.useState<{ data?: SegmentPreview; error?: ApiError; loading: boolean }>({ loading: false });
  const key = dsl ? JSON.stringify(dsl) : "";
  React.useEffect(() => {
    if (!key) return;
    const ctrl = new AbortController();
    setState((s) => ({ ...s, loading: true }));
    const t = setTimeout(() => {
      api<SegmentPreview>("/api/v1/segments/preview", { method: "POST", body: { rules: JSON.parse(key), channel: channel || null }, signal: ctrl.signal }).then(
        (data) => setState({ data, loading: false }),
        (err: unknown) => {
          if (ctrl.signal.aborted) return;
          setState({ error: toApiError(err), loading: false });
        },
      );
    }, 400);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [key, channel]);
  return state;
}

type Dispatch = React.Dispatch<Parameters<typeof reducer>[1]>;

function GroupEditor({ group, depth, catalogue, errors, dispatch, disabled, isRoot }: { group: GroupNode; depth: number; catalogue: FieldSpec[]; errors: Record<string, string>; dispatch: Dispatch; disabled: boolean; isRoot?: boolean }) {
  const full = countRules(group) >= MAX_RULES;
  return (
    <div className={isRoot ? "flex flex-col gap-2" : "flex flex-col gap-2 rounded-md border border-border bg-surface-2/40 p-3"} role="group" aria-label={`${group.op === "and" ? "All" : "Any"} of the following rules`}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted">Match</span>
        <label htmlFor={`${group.id}-op`} className="sr-only">Group operator</label>
        <Select id={`${group.id}-op`} className="w-24" value={group.op} disabled={disabled} onChange={(e) => dispatch({ type: "set_op", groupId: group.id, op: e.target.value as "and" | "or" })}>
          <option value="and">all</option>
          <option value="or">any</option>
        </Select>
        <span className="text-muted">of these rules</span>
        {!isRoot && !disabled ? (
          <Button type="button" size="sm" variant="ghost" className="ml-auto" aria-label="Remove group" onClick={() => dispatch({ type: "remove", id: group.id })}>
            <Trash2 className="h-4 w-4" aria-hidden />
          </Button>
        ) : null}
      </div>
      {group.children.length === 0 ? <p className="text-sm text-muted">{group.op === "and" ? "No rules: matches every contact." : "No rules: matches nobody."}</p> : null}
      {group.children.map((c: Node) =>
        c.kind === "group" ? (
          <GroupEditor key={c.id} group={c} depth={depth + 1} catalogue={catalogue} errors={errors} dispatch={dispatch} disabled={disabled} />
        ) : (
          <RuleEditor key={c.id} rule={c} catalogue={catalogue} error={errors[c.id]} dispatch={dispatch} disabled={disabled} />
        ),
      )}
      {!disabled ? (
        <div className="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="secondary" disabled={full} onClick={() => dispatch({ type: "add_rule", groupId: group.id })}>
            <Plus className="h-4 w-4" aria-hidden /> Rule
          </Button>
          {depth < MAX_DEPTH ? (
            <Button type="button" size="sm" variant="secondary" disabled={full} onClick={() => dispatch({ type: "add_group", groupId: group.id })}>
              <Plus className="h-4 w-4" aria-hidden /> Group
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function RuleEditor({ rule, catalogue, error, dispatch, disabled }: { rule: RuleNode; catalogue: FieldSpec[]; error?: string; dispatch: Dispatch; disabled: boolean }) {
  const spec = catalogue.find((s) => s.field === rule.field);
  const ops = spec?.operators ?? [rule.operator];
  const needsValue = !NO_VALUE.has(rule.operator);
  const isList = LIST_OPS.has(rule.operator);
  const errId = `${rule.id}-error`;
  return (
    <div className="flex flex-col gap-1">
      <div className="grid gap-2 sm:grid-cols-[minmax(0,12rem)_minmax(0,11rem)_minmax(0,1fr)_auto]">
        <label htmlFor={`${rule.id}-field`} className="sr-only">Field</label>
        <Select id={`${rule.id}-field`} value={rule.field} disabled={disabled} onChange={(e) => {
          const next = catalogue.find((s) => s.field === e.target.value);
          if (next) dispatch({ type: "set_field", id: rule.id, spec: next });
        }}>
          {!spec ? <option value={rule.field}>{rule.field} (unavailable)</option> : null}
          {catalogue.map((s) => <option key={s.field} value={s.field}>{s.label}</option>)}
        </Select>
        <label htmlFor={`${rule.id}-op`} className="sr-only">Operator</label>
        <Select id={`${rule.id}-op`} value={rule.operator} disabled={disabled} onChange={(e) => dispatch({ type: "set_operator", id: rule.id, operator: e.target.value as RuleNode["operator"] })}>
          {ops.map((o) => <option key={o} value={o}>{OPERATOR_LABEL[o] ?? o}</option>)}
        </Select>
        <label htmlFor={`${rule.id}-value`} className="sr-only">Value</label>
        {!needsValue ? <span aria-hidden /> : spec && (spec.kind === "enum" || spec.kind === "boolean") && !isList ? (
          <Select id={`${rule.id}-value`} value={rule.value} disabled={disabled} aria-invalid={error ? true : undefined} aria-describedby={error ? errId : undefined} onChange={(e) => dispatch({ type: "set_value", id: rule.id, value: e.target.value })}>
            <option value="">Choose…</option>
            {(spec.kind === "boolean" ? ["true", "false"] : spec.options ?? []).map((o) => <option key={o} value={o}>{spec.kind === "boolean" ? (o === "true" ? "Yes" : "No") : o.replace(/_/g, " ")}</option>)}
          </Select>
        ) : (
          <Input
            id={`${rule.id}-value`}
            value={rule.value}
            disabled={disabled}
            maxLength={isList ? 4000 : 200}
            type={!isList && spec?.kind === "datetime" ? "datetime-local" : !isList && spec?.kind === "date" ? "date" : !isList && spec?.kind === "number" ? "number" : "text"}
            placeholder={isList ? (spec?.options ? spec.options.join(", ") : "value, value") : ""}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errId : undefined}
            onChange={(e) => dispatch({ type: "set_value", id: rule.id, value: e.target.value })}
          />
        )}
        {!disabled ? (
          <Button type="button" size="sm" variant="ghost" aria-label="Remove rule" onClick={() => dispatch({ type: "remove", id: rule.id })}>
            <Trash2 className="h-4 w-4" aria-hidden />
          </Button>
        ) : null}
      </div>
      {error ? <p id={errId} className="text-xs text-danger">{error}</p> : null}
    </div>
  );
}
