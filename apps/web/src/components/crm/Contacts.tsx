"use client";
/** Contacts: search, filters, sort, cursor pagination, create. */
import { useRouter } from "next/navigation";
import Link from "next/link";
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useApi, useCursorList } from "@/lib/client/use-api";
import { relativeTime, formatDateTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, type Column, DataTable, EmptyState, Input, Select, StatusBadge, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { ContactDialog } from "./ContactDialog";
import { type Contact, contactName, type FieldDef, type Items, type Tag } from "./lib/types";

const SORTS = [
  { id: "created_at", label: "Created" },
  { id: "updated_at", label: "Updated" },
  { id: "last_name", label: "Last name" },
  { id: "company", label: "Company" },
] as const;

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = React.useState(value);
  React.useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function Contacts() {
  const can = useCan();
  const router = useRouter();
  const toast = useToast();
  const [q, setQ] = React.useState("");
  const [status, setStatus] = React.useState("");
  const [tag, setTag] = React.useState("");
  const [country, setCountry] = React.useState("");
  const [source, setSource] = React.useState("");
  const [sort, setSort] = React.useState("created_at");
  const [direction, setDirection] = React.useState<"asc" | "desc">("desc");
  const [creating, setCreating] = React.useState(false);
  const dq = useDebounced(q.trim());
  const dsource = useDebounced(source.trim());
  const countryOk = /^[A-Z]{2}$/.test(country) ? country : "";

  const list = useCursorList<Contact>(withQuery("/api/v1/contacts", { q: dq, status, tag, country: countryOk, source: dsource, sort, direction }));
  const tags = useApi<Items<Tag>>("/api/v1/tags");
  const fields = useApi<Items<FieldDef>>("/api/v1/custom-fields");
  const filtered = Boolean(dq || status || tag || countryOk || dsource);

  const columns: Column<Contact>[] = [
    {
      key: "name",
      header: "Contact",
      cell: (c) => (
        <Link href={`/app/crm/contacts/${c.id}`} className="block min-w-0 hover:underline">
          <span className="block truncate font-medium text-brand">{contactName(c)}</span>
          {c.company && contactName(c) !== c.company ? <span className="block truncate text-xs text-muted">{c.company}</span> : null}
        </Link>
      ),
    },
    { key: "email", header: "Email", cell: (c) => <span className="break-all">{c.email ?? "—"}</span>, hideOnMobile: true },
    { key: "phone", header: "Phone", cell: (c) => <span className="tabular whitespace-nowrap">{c.phone_e164 ?? "—"}</span>, hideOnMobile: true },
    { key: "country", header: "Country", cell: (c) => c.country ?? "—", hideOnMobile: true },
    { key: "tags", header: "Tags", cell: (c) => <div className="flex flex-wrap gap-1">{c.tags.slice(0, 4).map((t) => <Badge key={t}>{t}</Badge>)}{c.tags.length > 4 ? <Badge>+{c.tags.length - 4}</Badge> : null}</div> },
    { key: "status", header: "Status", cell: (c) => <StatusBadge status={c.status} /> },
    { key: "updated", header: "Updated", cell: (c) => <span title={formatDateTime(c.updated_at)}>{relativeTime(c.updated_at)}</span>, hideOnMobile: true },
  ];

  return (
    <Card>
      <CardHeader
        title="Contacts"
        description="People you can reach. Archived contacts are hidden unless you filter for them."
        actions={
          can("leads:create") ? (
            <Button size="sm" onClick={() => setCreating(true)} data-testid="new-contact">
              New contact
            </Button>
          ) : null
        }
      />
      <div className="flex flex-wrap items-end gap-2 border-b border-border px-4 py-3">
        <div className="w-full sm:w-64">
          <label htmlFor="contact-q" className="sr-only">
            Search contacts
          </label>
          <Input id="contact-q" type="search" placeholder="Search name, company, email, phone" value={q} maxLength={100} onChange={(e) => setQ(e.target.value)} />
        </div>
        <FilterSelect id="contact-status-filter" label="Status" value={status} onChange={setStatus} options={["active", "inactive", "archived"]} />
        <FilterSelect id="contact-tag-filter" label="Tag" value={tag} onChange={setTag} options={(tags.data?.items ?? []).map((t) => t.name)} />
        <div className="w-24">
          <label htmlFor="contact-country" className="sr-only">
            Country
          </label>
          <Input id="contact-country" placeholder="Country" maxLength={2} value={country} onChange={(e) => setCountry(e.target.value.toUpperCase())} />
        </div>
        <div className="w-32">
          <label htmlFor="contact-source" className="sr-only">
            Source
          </label>
          <Input id="contact-source" placeholder="Source" maxLength={64} value={source} onChange={(e) => setSource(e.target.value)} />
        </div>
        <SortControl sort={sort} direction={direction} onSort={setSort} onDirection={setDirection} options={SORTS} idPrefix="contact" />
      </div>
      <DataTable
        caption="Contacts"
        columns={columns}
        rows={list.items}
        rowKey={(c) => c.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        hasMore={list.hasMore}
        loadingMore={list.loadingMore}
        onLoadMore={list.loadMore}
        empty={
          <EmptyState
            title={filtered ? "No matching contacts" : "No contacts yet"}
            description={filtered ? "Try other filters." : "Create a contact or import a CSV file."}
            action={!filtered && can("leads:create") ? <Link href="/app/crm/imports" className="text-sm font-medium text-brand hover:underline">Import contacts</Link> : undefined}
          />
        }
      />
      <ContactDialog
        open={creating}
        onClose={() => setCreating(false)}
        contact={null}
        fields={fields.data?.items ?? []}
        onSaved={(c) => {
          setCreating(false);
          toast({ tone: "success", title: "Contact created" });
          router.push(`/app/crm/contacts/${c.id}`);
        }}
      />
    </Card>
  );
}

export function FilterSelect({
  id,
  label,
  value,
  onChange,
  options,
  allLabel = "All",
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: readonly (string | { value: string; label: string })[];
  allLabel?: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <label htmlFor={id} className="text-sm text-muted">
        {label}
      </label>
      <Select id={id} value={value} onChange={(e) => onChange(e.target.value)} className="w-36">
        <option value="">{allLabel}</option>
        {options.map((o) => {
          const v = typeof o === "string" ? o : o.value;
          const l = typeof o === "string" ? o.replace(/_/g, " ") : o.label;
          return (
            <option key={v} value={v}>
              {l}
            </option>
          );
        })}
      </Select>
    </div>
  );
}

export function SortControl({
  sort,
  direction,
  onSort,
  onDirection,
  options,
  idPrefix,
}: {
  sort: string;
  direction: "asc" | "desc";
  onSort: (s: string) => void;
  onDirection: (d: "asc" | "desc") => void;
  options: readonly { id: string; label: string }[];
  idPrefix: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <label htmlFor={`${idPrefix}-sort`} className="text-sm text-muted">
        Sort
      </label>
      <Select id={`${idPrefix}-sort`} value={sort} onChange={(e) => onSort(e.target.value)} className="w-32">
        {options.map((o) => (
          <option key={o.id} value={o.id}>
            {o.label}
          </option>
        ))}
      </Select>
      <Button
        type="button"
        size="sm"
        variant="secondary"
        aria-label={direction === "desc" ? "Sorted descending; switch to ascending" : "Sorted ascending; switch to descending"}
        onClick={() => onDirection(direction === "desc" ? "asc" : "desc")}
      >
        {direction === "desc" ? "↓" : "↑"}
      </Button>
    </div>
  );
}
