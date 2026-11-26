"use client";
/** Custom field definitions and tags. Field keys and types are fixed after creation (values depend on them). */
import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { relativeTime } from "@/lib/format";
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
  Field,
  Input,
  Select,
  Tabs,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { MutationError } from "./common";
import type { FieldDef, FieldType, Items, Tag } from "./lib/types";

export function FieldsAndTags() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = params.get("tab") === "tags" ? "tags" : "fields";
  return (
    <Tabs
      label="Fields and tags"
      value={tab}
      onChange={(t) => router.replace(t === "tags" ? "/app/crm/fields?tab=tags" : "/app/crm/fields")}
      tabs={[{ id: "fields", label: "Custom fields" }, { id: "tags", label: "Tags" }]}
    >
      {tab === "fields" ? <CustomFields /> : <Tags />}
    </Tabs>
  );
}

const TYPES: FieldType[] = ["text", "number", "boolean", "date", "select"];

/** Splits the options textarea (one per line or comma separated) into unique trimmed options. */
export function parseOptions(text: string): string[] {
  return [...new Set(text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean))];
}

function CustomFields() {
  const can = useCan();
  const toast = useToast();
  const list = useApi<Items<FieldDef>>("/api/v1/custom-fields");
  const [editing, setEditing] = React.useState<FieldDef | "new" | null>(null);
  const [removing, setRemoving] = React.useState<FieldDef | null>(null);
  const edit = can("leads:update");
  const columns: Column<FieldDef>[] = [
    { key: "label", header: "Label", cell: (f) => <span className="font-medium">{f.label}</span> },
    { key: "key", header: "Key", cell: (f) => <code className="text-xs">custom.{f.key}</code> },
    { key: "type", header: "Type", cell: (f) => <Badge>{f.type}</Badge> },
    { key: "options", header: "Options", cell: (f) => (f.options.length ? <span className="line-clamp-1 text-muted">{f.options.join(", ")}</span> : "—"), hideOnMobile: true },
    { key: "required", header: "Required", cell: (f) => (f.required ? "Yes" : "No"), hideOnMobile: true },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (f) =>
        edit ? (
          <div className="flex justify-end gap-1">
            <Button size="sm" variant="secondary" onClick={() => setEditing(f)}>Edit</Button>
            <Button size="sm" variant="ghost" onClick={() => setRemoving(f)}>Delete</Button>
          </div>
        ) : null,
    },
  ];
  return (
    <Card>
      <CardHeader title="Custom fields" description="Up to 50 per workspace. Use them in imports (custom.<key>), on contacts and in segments." actions={edit ? <Button size="sm" onClick={() => setEditing("new")} data-testid="new-field">New field</Button> : null} />
      <DataTable caption="Custom fields" columns={columns} rows={list.data?.items ?? []} rowKey={(f) => f.id} loading={list.loading} error={list.error} onRetry={list.reload} empty={<EmptyState title="No custom fields" />} />
      <FieldDialog
        field={editing}
        onClose={() => setEditing(null)}
        onReload={list.reload}
        onSaved={() => {
          setEditing(null);
          list.reload();
          toast({ tone: "success", title: "Custom field saved" });
        }}
      />
      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title="Delete custom field"
        description="Existing values stay on contacts, but they can no longer be set or used in segments. Segments using this field stop validating."
        confirmLabel="Delete"
        typedConfirmation={removing?.key}
        onConfirm={async () => {
          if (!removing) return;
          await api(`/api/v1/custom-fields/${removing.id}`, { method: "DELETE" });
          list.reload();
          toast({ tone: "success", title: "Custom field deleted" });
        }}
      />
    </Card>
  );
}

function FieldDialog({ field, onClose, onSaved, onReload }: { field: FieldDef | "new" | null; onClose: () => void; onSaved: () => void; onReload: () => void }) {
  const isNew = field === "new";
  const existing = field && field !== "new" ? field : null;
  const [key, setKey] = React.useState("");
  const [label, setLabel] = React.useState("");
  const [type, setType] = React.useState<FieldType>("text");
  const [options, setOptions] = React.useState("");
  const [required, setRequired] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    if (!field) return;
    setKey(existing?.key ?? "");
    setLabel(existing?.label ?? "");
    setType(existing?.type ?? "text");
    setOptions(existing?.options.join("\n") ?? "");
    setRequired(existing?.required ?? false);
    setError(null);
  }, [field, existing]);
  const keyOk = /^[a-z][a-z0-9_]{0,63}$/.test(key);
  const opts = parseOptions(options);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (existing) {
        const body: Record<string, unknown> = { expected_version: existing.version };
        if (label.trim() !== existing.label) body.label = label.trim();
        if (required !== existing.required) body.required = required;
        if (existing.type === "select" && opts.join("\n") !== existing.options.join("\n")) body.options = opts;
        await api(`/api/v1/custom-fields/${existing.id}`, { method: "PATCH", body });
      } else {
        await api("/api/v1/custom-fields", { method: "POST", body: { key, label: label.trim(), type, required, ...(type === "select" ? { options: opts } : {}) } });
      }
      onSaved();
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={field !== null} onClose={onClose} busy={busy} title={isNew ? "New custom field" : "Edit custom field"}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="cf-key" label="Key" required hint="Lowercase letters, digits and _; starts with a letter. Cannot change later." error={key && !keyOk ? "Invalid key" : error?.fields.key}>
          {(p) => <Input {...p} value={key} disabled={!isNew} maxLength={64} onChange={(e) => setKey(e.target.value.toLowerCase())} className="font-mono" />}
        </Field>
        <Field id="cf-label" label="Label" required error={error?.fields.label}>
          {(p) => <Input {...p} value={label} maxLength={120} onChange={(e) => setLabel(e.target.value)} />}
        </Field>
        <Field id="cf-type" label="Type" error={error?.fields.type}>
          {(p) => <Select {...p} value={type} disabled={!isNew} onChange={(e) => setType(e.target.value as FieldType)}>{TYPES.map((t) => <option key={t} value={t}>{t}</option>)}</Select>}
        </Field>
        {type === "select" ? (
          <Field id="cf-options" label="Options" hint="One per line (1–100 options)." error={error?.fields.options}>
            {(p) => <Textarea {...p} rows={4} value={options} onChange={(e) => setOptions(e.target.value)} />}
          </Field>
        ) : null}
        <Checkbox label="Required on new contacts" checked={required} onChange={(e) => setRequired(e.target.checked)} />
        <MutationError error={error} onReload={() => { onClose(); onReload(); }} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy || !keyOk || !label.trim() || (type === "select" && (opts.length === 0 || opts.length > 100))}>{busy ? "Saving…" : "Save"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function Tags() {
  const can = useCan();
  const toast = useToast();
  const list = useApi<Items<Tag>>("/api/v1/tags");
  const [name, setName] = React.useState("");
  const [color, setColor] = React.useState("#4f46e5");
  const [useColor, setUseColor] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [removing, setRemoving] = React.useState<Tag | null>(null);
  const edit = can("leads:update");

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/tags", { method: "POST", body: { name: name.trim(), color: useColor ? color : null } });
      setName("");
      list.reload();
      toast({ tone: "success", title: "Tag created" });
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<Tag>[] = [
    { key: "name", header: "Tag", cell: (t) => <span className="inline-flex items-center gap-2">{t.color ? <span aria-hidden className="h-3 w-3 rounded-full" style={{ backgroundColor: t.color }} /> : null}{t.name}</span> },
    { key: "created", header: "Created", cell: (t) => relativeTime(t.created_at), hideOnMobile: true },
    { key: "actions", header: <span className="sr-only">Actions</span>, className: "text-right", cell: (t) => (edit ? <Button size="sm" variant="ghost" onClick={() => setRemoving(t)}>Delete</Button> : null) },
  ];

  return (
    <Card>
      <CardHeader title="Tags" description="Labels for contacts, usable in filters and segments." />
      {edit ? (
        <form onSubmit={create} className="flex flex-wrap items-end gap-3 border-b border-border px-5 py-4">
          <Field id="tag-name" label="New tag" error={error?.fields.name} className="w-56">
            {(p) => <Input {...p} maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />}
          </Field>
          <Checkbox label="Colour" checked={useColor} onChange={(e) => setUseColor(e.target.checked)} className="mb-2" />
          {useColor ? (
            <div className="mb-0.5">
              <label htmlFor="tag-color" className="sr-only">Tag colour</label>
              <input id="tag-color" type="color" value={color} onChange={(e) => setColor(e.target.value)} className="h-9 w-12 rounded border border-border bg-surface" />
            </div>
          ) : null}
          <Button type="submit" disabled={busy || !name.trim()}>{busy ? "Adding…" : "Add tag"}</Button>
          <div className="w-full"><MutationError error={error} /></div>
        </form>
      ) : null}
      <DataTable caption="Tags" columns={columns} rows={list.data?.items ?? []} rowKey={(t) => t.id} loading={list.loading} error={list.error} onRetry={list.reload} empty={<EmptyState title="No tags yet" />} />
      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title="Delete tag"
        description={`"${removing?.name ?? ""}" is removed from every contact. Segments that use it stop matching on it.`}
        confirmLabel="Delete"
        onConfirm={async () => {
          if (!removing) return;
          await api(`/api/v1/tags/${removing.id}`, { method: "DELETE" });
          list.reload();
          toast({ tone: "success", title: "Tag deleted" });
        }}
      />
    </Card>
  );
}
