"use client";
/**
 * Email senders and domains (emails:campaigns:manage). A domain sends only once DKIM verifies; SPF and DMARC
 * are shown as guidance. DNS records are public values and can be copied. Inbound mailboxes (inbox
 * service) are configured here too.
 */
import { Check, Copy, RefreshCw } from "lucide-react";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { useCan } from "@/components/shell/session-context";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  type Column,
  ConfirmDialog,
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
  Select,
  StatusBadge,
  Tabs,
  useToast,
} from "@/components/ui";
import { AgentSelect, useAgentNames } from "@/components/calls/pickers";
import { useIdempotencyKey } from "@/components/calls/use-live";
import { dnsLabel, dnsTone } from "./logic";
import type { Domain, Mailbox, Sender } from "./types";

export function EmailAdmin({ initialTab }: { initialTab?: string }) {
  const [tab, setTab] = React.useState(["domains", "senders", "mailboxes"].includes(initialTab ?? "") ? initialTab! : "domains");
  return (
    <Tabs
      label="Email administration"
      value={tab}
      onChange={setTab}
      tabs={[
        { id: "domains", label: "Sending domains" },
        { id: "senders", label: "Sender identities" },
        { id: "mailboxes", label: "Inbound mailboxes" },
      ]}
    >
      {tab === "domains" ? <Domains /> : tab === "senders" ? <Senders /> : <Mailboxes />}
    </Tabs>
  );
}

function useCanManage() {
  return useCan()("emails:campaigns:manage");
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [done, setDone] = React.useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        } catch {
          setDone(false);
        }
      }}
      className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-2"
      aria-label={`Copy ${label}`}
    >
      {done ? <Check className="h-3.5 w-3.5 text-ok" aria-hidden /> : <Copy className="h-3.5 w-3.5" aria-hidden />}
      {done ? "Copied" : "Copy"}
    </button>
  );
}

function Domains() {
  const manage = useCanManage();
  const toast = useToast();
  const list = useCursorList<Domain>("/api/v1/email/domains");
  const [adding, setAdding] = React.useState(false);
  const [deleting, setDeleting] = React.useState<Domain | null>(null);
  const [verifying, setVerifying] = React.useState<string | null>(null);

  async function verify(d: Domain) {
    setVerifying(d.id);
    try {
      const out = await api<Domain>(`/api/v1/email/domains/${d.id}/verify`, { method: "POST" });
      toast(
        out.status === "verified"
          ? { tone: "success", title: `${out.domain} is verified`, description: "Sender identities on this domain can now send." }
          : { tone: "error", title: `${out.domain} is not verified yet`, description: "DNS changes can take up to 48 hours. Check the DKIM records and try again." },
      );
      list.reload();
    } catch (err) {
      const e = toApiError(err);
      if (!e.isStepUp) toast({ tone: "error", title: "Verification failed", description: e.message });
    } finally {
      setVerifying(null);
    }
  }

  if (list.loading && list.items.length === 0) return <Card><LoadingState label="Loading domains" /></Card>;
  if (list.error && list.items.length === 0) return <Card><ErrorState problem={list.error.problem} onRetry={list.reload} /></Card>;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-2xl text-sm text-muted">Add the DNS records at your DNS host, then verify. Only verified domains can send. DKIM is required; SPF and DMARC improve deliverability.</p>
        {manage ? (
          <Button size="sm" onClick={() => setAdding(true)} data-testid="add-domain">
            Add domain
          </Button>
        ) : null}
      </div>
      {list.items.length === 0 ? (
        <Card>
          <EmptyState title="No sending domains" description="Add the domain you send from (for example mail.example.com) to get its DNS records." />
        </Card>
      ) : null}
      {list.items.map((d) => (
        <Card key={d.id} data-testid="domain-card">
          <CardHeader
            title={d.domain}
            description={d.last_checked_at ? `Last checked ${relativeTime(d.last_checked_at)}${d.verified_at ? ` · verified ${formatDateTime(d.verified_at)}` : ""}` : "Not checked yet"}
            actions={
              <div className="flex items-center gap-2">
                <Badge tone={dnsTone(d.status)}>{d.status === "verified" ? "Can send" : d.status === "disabled" ? "Disabled" : "Cannot send yet"}</Badge>
                {manage && d.status !== "disabled" ? (
                  <Button size="sm" variant="secondary" onClick={() => verify(d)} disabled={verifying === d.id}>
                    <RefreshCw className={verifying === d.id ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} aria-hidden />
                    {verifying === d.id ? "Checking…" : "Verify DNS"}
                  </Button>
                ) : null}
                {manage ? (
                  <Button size="sm" variant="ghost" onClick={() => setDeleting(d)}>
                    Remove
                  </Button>
                ) : null}
              </div>
            }
          />
          <div className="flex flex-wrap gap-2 border-b border-border px-5 py-3 text-sm">
            {(
              [
                ["DKIM", d.dkim_status, "required"],
                ["SPF", d.spf_status, "recommended"],
                ["DMARC", d.dmarc_status, "recommended"],
              ] as const
            ).map(([name, status, need]) => (
              <span key={name} className="inline-flex items-center gap-1.5">
                <span className="font-medium">{name}</span>
                <Badge tone={dnsTone(status)}>{dnsLabel(status)}</Badge>
                <span className="text-xs text-muted">({need})</span>
              </span>
            ))}
          </div>
          {d.records.length === 0 ? (
            <p className="px-5 py-3 text-sm text-muted">No DNS records were returned for this domain.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">DNS records for {d.domain}</caption>
                <thead>
                  <tr className="text-xs uppercase tracking-wide text-muted">
                    <th className="px-5 py-2">Purpose</th>
                    <th className="px-2 py-2">Type</th>
                    <th className="px-2 py-2">Name / host</th>
                    <th className="px-2 py-2">Value</th>
                  </tr>
                </thead>
                <tbody>
                  {d.records.map((r, i) => (
                    <tr key={`${r.type}-${r.name}-${i}`} className="border-t border-border align-top">
                      <td className="px-5 py-2">{r.purpose.toUpperCase()}</td>
                      <td className="px-2 py-2 font-mono text-xs">{r.type}</td>
                      <td className="px-2 py-2">
                        <div className="flex items-start gap-2">
                          <code className="min-w-0 break-all font-mono text-xs">{r.name}</code>
                          <CopyButton value={r.name} label={`${r.purpose} record name`} />
                        </div>
                      </td>
                      <td className="px-2 py-2">
                        <div className="flex items-start gap-2">
                          <code className="min-w-0 break-all font-mono text-xs">{r.value}</code>
                          <CopyButton value={r.value} label={`${r.purpose} record value`} />
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      ))}
      {list.hasMore ? (
        <div className="flex justify-center">
          <Button variant="secondary" size="sm" onClick={list.loadMore} disabled={list.loadingMore}>
            Load more
          </Button>
        </div>
      ) : null}
      <AddDomainDialog
        open={adding}
        onClose={() => setAdding(false)}
        onAdded={(d) => {
          setAdding(false);
          list.reload();
          toast({ tone: "success", title: `${d.domain} added`, description: "Add the DNS records shown, then verify." });
        }}
      />
      <ConfirmDialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title="Remove this domain?"
        description="Sender identities on it must be removed or disabled first. Campaigns using it can no longer send."
        confirmLabel="Remove domain"
        typedConfirmation={deleting?.domain}
        onConfirm={async () => {
          if (!deleting) return;
          await api(`/api/v1/email/domains/${deleting.id}`, { method: "DELETE" });
          list.reload();
          toast({ tone: "success", title: "Domain removed" });
        }}
      />
    </div>
  );
}

function AddDomainDialog({ open, onClose, onAdded }: { open: boolean; onClose: () => void; onAdded: (d: Domain) => void }) {
  const key = useIdempotencyKey(open);
  const [domain, setDomain] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    if (open) {
      setDomain("");
      setError(null);
    }
  }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onAdded(await api<Domain>("/api/v1/email/domains", { method: "POST", body: { domain: domain.trim().toLowerCase() }, idempotencyKey: key || true }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} busy={busy} title="Add a sending domain" description="Use a domain you control. A subdomain such as mail.example.com keeps marketing mail separate.">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field id="domain-name" label="Domain" required error={error?.fields.domain ?? (error && error.status === 422 ? error.message : undefined)}>
          {(p) => <Input {...p} value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="mail.example.com" autoComplete="off" spellCheck={false} />}
        </Field>
        <FormError message={error && error.status !== 422 && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || domain.trim().length < 3}>
            {busy ? "Adding…" : "Add domain"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function Senders() {
  const manage = useCanManage();
  const toast = useToast();
  const list = useCursorList<Sender>("/api/v1/email/senders");
  const domains = useCursorList<Domain>("/api/v1/email/domains");
  const [editing, setEditing] = React.useState<Sender | "new" | null>(null);

  const columns: Column<Sender>[] = [
    {
      key: "from",
      header: "From",
      cell: (s) => (
        <div>
          <span className="font-medium">{s.from_name}</span>
          <span className="block font-mono text-xs text-muted">{s.from_address}</span>
        </div>
      ),
    },
    { key: "reply", header: "Reply-To", cell: (s) => <span className="font-mono text-xs">{s.reply_to ?? "—"}</span>, hideOnMobile: true },
    { key: "domain", header: "Domain", cell: (s) => <span className="inline-flex items-center gap-1.5">{s.domain}<Badge tone={dnsTone(s.domain_status)}>{s.domain_status}</Badge></span> },
    { key: "status", header: "Status", cell: (s) => <StatusBadge status={s.status === "disabled" ? "disabled" : s.domain_status === "verified" ? "active" : "pending"} /> },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (s) =>
        manage ? (
          <Button size="sm" variant="secondary" onClick={() => setEditing(s)}>
            Edit
          </Button>
        ) : null,
    },
  ];

  return (
    <>
      <Card>
        <CardHeader
          title="Sender identities"
          description="The From name and address recipients see. A sender can be used only when its domain is verified."
          actions={
            manage ? (
              <Button size="sm" onClick={() => setEditing("new")} disabled={domains.items.length === 0}>
                Add sender
              </Button>
            ) : null
          }
        />
        <DataTable
          caption="Sender identities"
          columns={columns}
          rows={list.items}
          rowKey={(s) => s.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          onLoadMore={list.loadMore}
          empty={<EmptyState title="No sender identities" description={domains.items.length === 0 ? "Add a sending domain first." : "Add the From address campaigns are sent from."} />}
        />
      </Card>
      <SenderDialog
        target={editing}
        domains={domains.items}
        onClose={() => setEditing(null)}
        onSaved={(created) => {
          setEditing(null);
          list.reload();
          toast({ tone: "success", title: created ? "Sender added" : "Sender updated" });
        }}
        onConflict={() => {
          setEditing(null);
          list.reload();
        }}
      />
    </>
  );
}

function SenderDialog({ target, domains, onClose, onSaved, onConflict }: { target: Sender | "new" | null; domains: Domain[]; onClose: () => void; onSaved: (created: boolean) => void; onConflict: () => void }) {
  const creating = target === "new";
  const existing = target && target !== "new" ? target : null;
  const key = useIdempotencyKey(target !== null);
  const [domainId, setDomainId] = React.useState("");
  const [localPart, setLocalPart] = React.useState("");
  const [fromName, setFromName] = React.useState("");
  const [replyTo, setReplyTo] = React.useState("");
  const [status, setStatus] = React.useState("active");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  React.useEffect(() => {
    setError(null);
    setDomainId(existing?.domain_id ?? domains.find((d) => d.status === "verified")?.id ?? domains[0]?.id ?? "");
    setLocalPart("");
    setFromName(existing?.from_name ?? "");
    setReplyTo(existing?.reply_to ?? "");
    setStatus(existing?.status ?? "active");
  }, [target]); // eslint-disable-line react-hooks/exhaustive-deps

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (creating) {
        await api("/api/v1/email/senders", {
          method: "POST",
          body: { domain_id: domainId, local_part: localPart.trim(), from_name: fromName.trim(), reply_to: replyTo.trim() || null },
          idempotencyKey: key || true,
        });
      } else if (existing) {
        await api(`/api/v1/email/senders/${existing.id}`, {
          method: "PATCH",
          body: { expected_version: existing.version, from_name: fromName.trim(), reply_to: replyTo.trim() || null, status },
        });
      }
      onSaved(creating);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  const domain = domains.find((d) => d.id === domainId);
  const stale = error?.isConflict && !creating;
  return (
    <Dialog open={target !== null} onClose={onClose} busy={busy} title={creating ? "Add a sender identity" : `Edit ${existing?.from_address ?? ""}`}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        {stale ? <ConflictNotice onReload={onConflict} /> : null}
        {creating ? (
          <div className="grid grid-cols-[1fr_auto_1fr] items-end gap-2">
            <Field id="sender-local" label="Address" required error={error?.fields.local_part}>
              {(p) => <Input {...p} value={localPart} maxLength={64} onChange={(e) => setLocalPart(e.target.value)} placeholder="hello" autoComplete="off" spellCheck={false} />}
            </Field>
            <span className="pb-2 text-muted">@</span>
            <Field id="sender-domain" label="Domain" required error={error?.fields.domain_id}>
              {(p) => (
                <Select {...p} value={domainId} onChange={(e) => setDomainId(e.target.value)}>
                  {domains.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.domain}
                      {d.status !== "verified" ? " (not verified)" : ""}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>
        ) : null}
        {creating && domain && domain.status !== "verified" ? <Notice tone="warn">This domain is not verified. The sender is saved but cannot send until the domain verifies.</Notice> : null}
        <Field id="sender-name" label="From name" required error={error?.fields.from_name}>
          {(p) => <Input {...p} value={fromName} maxLength={120} onChange={(e) => setFromName(e.target.value)} placeholder="Acme Home Services" />}
        </Field>
        <Field id="sender-reply" label="Reply-To (optional)" hint="Replies go here. Use an inbound mailbox address to get replies in the Inbox." error={error?.fields.reply_to}>
          {(p) => <Input {...p} type="email" value={replyTo} maxLength={320} onChange={(e) => setReplyTo(e.target.value)} autoComplete="off" />}
        </Field>
        {!creating ? (
          <Field id="sender-status" label="Status">
            {(p) => (
              <Select {...p} value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="active">Active</option>
                <option value="disabled">Disabled</option>
              </Select>
            )}
          </Field>
        ) : null}
        <FormError message={error && !stale && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !fromName.trim() || (creating && (!localPart.trim() || !domainId))}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function Mailboxes() {
  const manage = useCanManage();
  const toast = useToast();
  const list = useCursorList<Mailbox>("/api/v1/email/mailboxes");
  const agentName = useAgentNames();
  const [editing, setEditing] = React.useState<Mailbox | "new" | null>(null);
  const columns: Column<Mailbox>[] = [
    {
      key: "address",
      header: "Address",
      cell: (m) => (
        <div>
          <span className="font-mono text-sm">{m.address}</span>
          {m.display_name ? <span className="block text-xs text-muted">{m.display_name}</span> : null}
        </div>
      ),
    },
    { key: "agent", header: "Drafting agent", cell: (m) => (m.agent_id ? agentName(m.agent_id) : "—") },
    { key: "auto", header: "AI drafts", cell: (m) => (m.auto_draft ? <Badge tone="brand">Drafts for review</Badge> : <Badge>Off</Badge>) },
    { key: "status", header: "Status", cell: (m) => <StatusBadge status={m.status} /> },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (m) =>
        manage ? (
          <Button size="sm" variant="secondary" onClick={() => setEditing(m)}>
            Edit
          </Button>
        ) : null,
    },
  ];
  return (
    <>
      <Card>
        <CardHeader
          title="Inbound mailboxes"
          description="Replies to these addresses arrive in the Inbox. With AI drafts on, an agent writes a reply that a person must approve before it is sent."
          actions={
            manage ? (
              <Button size="sm" onClick={() => setEditing("new")}>
                Add mailbox
              </Button>
            ) : null
          }
        />
        <DataTable
          caption="Inbound mailboxes"
          columns={columns}
          rows={list.items}
          rowKey={(m) => m.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          onLoadMore={list.loadMore}
          empty={<EmptyState title="No inbound mailboxes" description="Add the address customers reply to." />}
        />
      </Card>
      <MailboxDialog
        target={editing}
        onClose={() => setEditing(null)}
        onSaved={(created) => {
          setEditing(null);
          list.reload();
          toast({ tone: "success", title: created ? "Mailbox added" : "Mailbox updated" });
        }}
        onConflict={() => {
          setEditing(null);
          list.reload();
        }}
      />
    </>
  );
}

function MailboxDialog({ target, onClose, onSaved, onConflict }: { target: Mailbox | "new" | null; onClose: () => void; onSaved: (created: boolean) => void; onConflict: () => void }) {
  const creating = target === "new";
  const existing = target && target !== "new" ? target : null;
  const key = useIdempotencyKey(target !== null);
  const [address, setAddress] = React.useState("");
  const [displayName, setDisplayName] = React.useState("");
  const [agentId, setAgentId] = React.useState("");
  const [autoDraft, setAutoDraft] = React.useState(true);
  const [status, setStatus] = React.useState("active");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  React.useEffect(() => {
    setError(null);
    setAddress(existing?.address ?? "");
    setDisplayName(existing?.display_name ?? "");
    setAgentId(existing?.agent_id ?? "");
    setAutoDraft(existing?.auto_draft ?? true);
    setStatus(existing?.status ?? "active");
  }, [target]); // eslint-disable-line react-hooks/exhaustive-deps

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (creating)
        await api("/api/v1/email/mailboxes", { method: "POST", body: { address: address.trim(), display_name: displayName.trim(), agent_id: agentId || null, auto_draft: autoDraft }, idempotencyKey: key || true });
      else if (existing)
        await api(`/api/v1/email/mailboxes/${existing.id}`, { method: "PATCH", body: { expected_version: existing.version, display_name: displayName.trim(), agent_id: agentId || null, auto_draft: autoDraft, status } });
      onSaved(creating);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }
  const stale = error?.isConflict && !creating;
  return (
    <Dialog open={target !== null} onClose={onClose} busy={busy} title={creating ? "Add an inbound mailbox" : `Edit ${existing?.address ?? ""}`}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        {stale ? <ConflictNotice onReload={onConflict} /> : null}
        <Field id="mb-address" label="Address" required error={error?.fields.address} hint={creating ? undefined : "The address cannot be changed."}>
          {(p) => <Input {...p} type="email" value={address} disabled={!creating} onChange={(e) => setAddress(e.target.value)} autoComplete="off" spellCheck={false} />}
        </Field>
        <Field id="mb-name" label="Display name" error={error?.fields.display_name}>
          {(p) => <Input {...p} value={displayName} maxLength={200} onChange={(e) => setDisplayName(e.target.value)} />}
        </Field>
        <Field id="mb-agent" label="Drafting agent (optional)" error={error?.fields.agent_id}>
          {(p) => <AgentSelect kind="email" control={p} value={agentId} onChange={setAgentId} allowNone />}
        </Field>
        <Checkbox checked={autoDraft} onChange={(e) => setAutoDraft(e.target.checked)} label="Write AI reply drafts" description="Drafts always wait in the approval queue. Nothing is sent without a person approving it." />
        {!creating ? (
          <Field id="mb-status" label="Status">
            {(p) => (
              <Select {...p} value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="active">Active</option>
                <option value="disabled">Disabled</option>
              </Select>
            )}
          </Field>
        ) : null}
        <FormError message={error && !stale && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || (creating && address.trim().length < 3)}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
