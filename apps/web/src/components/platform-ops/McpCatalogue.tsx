"use client";
/**
 * MCP servers and the tool catalogue. Servers must be pinned to a reviewed version (name, version, sha256)
 * before they can be enabled; tools carry a risk class (class 3 always needs approval, class 4 can never be
 * granted to an agent). Every change needs `platform:ai:manage` and a recent step-up and is audited.
 */
import { Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
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
  LoadingState,
  Select,
  Tabs,
  Textarea,
  useToast,
} from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { StepUpHint } from "./common";
import { type McpServer, parseHosts, RISK_CLASSES, riskInfo, serverState, type ToolDef, toolState, TRUST_LEVELS, validatePin } from "./lib/mcp";
import { CLASSIFICATIONS } from "./lib/ai";

export function McpCatalogue({ initialTab }: { initialTab?: string }) {
  const [tab, setTab] = React.useState(initialTab === "tools" ? "tools" : "servers");
  return (
    <Tabs
      label="MCP and tools"
      value={tab}
      onChange={setTab}
      tabs={[
        { id: "servers", label: "MCP servers" },
        { id: "tools", label: "Tool catalogue" },
      ]}
    >
      {tab === "servers" ? <Servers /> : <Tools />}
    </Tabs>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function Servers() {
  const can = useCan();
  const servers = useApi<{ items: McpServer[] }>("/api/v1/platform/ai/mcp-servers");
  const [creating, setCreating] = React.useState(false);
  const columns: Column<McpServer>[] = [
    {
      key: "name",
      header: "Server",
      cell: (s) => (
        <Link href={`/platform/ai/tools/servers/${s.id}`} className="text-brand hover:underline">
          <span className="font-medium">{s.display_name}</span>
          <span className="block font-mono text-xs text-muted">{s.key}</span>
        </Link>
      ),
    },
    {
      key: "state",
      header: "State",
      cell: (s) => {
        const st = serverState(s);
        return <Badge tone={st.tone}>{st.label}</Badge>;
      },
    },
    { key: "trust", header: "Trust", hideOnMobile: true, cell: (s) => TRUST_LEVELS.find((t) => t.value === s.trust_level)?.label ?? s.trust_level },
    { key: "pin", header: "Pinned version", hideOnMobile: true, cell: (s) => <span className="font-mono text-xs">{s.pinned_version ?? "—"}</span> },
    { key: "host", header: "Host", hideOnMobile: true, cell: (s) => <span className="font-mono text-xs">{hostOf(s.endpoint)}</span> },
    { key: "env", header: "Env", hideOnMobile: true, cell: (s) => s.environment },
  ];
  return (
    <Card>
      <CardHeader
        title="MCP servers"
        description="Approved remote tool servers. Changing a server's endpoint, hosts, credential, identity or trust level clears its pin and disables it until re-reviewed."
        actions={
          can("platform:ai:manage") ? (
            <Button onClick={() => setCreating(true)} data-testid="new-mcp-server">
              <Plus className="h-4 w-4" aria-hidden /> Register server
            </Button>
          ) : null
        }
      />
      {servers.loading && !servers.data ? (
        <LoadingState label="Loading servers" />
      ) : servers.error && !servers.data ? (
        <ErrorState problem={servers.error.problem} onRetry={servers.reload} />
      ) : (
        <DataTable caption="MCP servers" columns={columns} rows={servers.data?.items ?? []} rowKey={(s) => s.id} empty={<EmptyState title="No MCP servers registered" />} />
      )}
      {creating ? <CreateServerDialog onClose={() => setCreating(false)} /> : null}
    </Card>
  );
}

function CreateServerDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const toast = useToast();
  const [f, setF] = React.useState({
    key: "",
    display_name: "",
    purpose: "",
    owner: "",
    environment: "dev",
    endpoint: "",
    allowed_hosts: "",
    service_identity: "",
    trust_level: "verified",
    vault_ref: "",
    pin_name: "",
    pin_version: "",
    pin_sha: "",
  });
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => setF((s) => ({ ...s, [k]: e.target.value }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const local: Record<string, string> = {};
    if (!/^[a-z][a-z0-9-]{1,62}$/.test(f.key)) local.key = "Lowercase letters, digits and dashes, starting with a letter.";
    if (f.display_name.trim().length < 2) local.display_name = "At least 2 characters.";
    if (f.purpose.trim().length < 5) local.purpose = "At least 5 characters.";
    if (f.owner.trim().length < 2) local.owner = "Who is accountable for this server.";
    if (!/^[a-z0-9-]{2,32}$/.test(f.environment)) local.environment = "e.g. dev, staging, prod.";
    if (!/^https:\/\/[^?#]+$/i.test(f.endpoint.trim())) local.endpoint = "An https:// URL without a query string.";
    const hosts = parseHosts(f.allowed_hosts);
    if (hosts.length === 0) local.allowed_hosts = "At least one host.";
    if (!/^[a-z][a-z0-9-]{2,63}$/.test(f.service_identity)) local.service_identity = "A dedicated identity, e.g. mcp-crm-x.";
    const wantsPin = Boolean(f.pin_name || f.pin_version || f.pin_sha);
    if (wantsPin) {
      const pe = validatePin({ server_name: f.pin_name, version: f.pin_version, sha256: f.pin_sha });
      for (const [k, v] of Object.entries(pe)) local[`pin.${k}`] = v;
    }
    setErrors(local);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api<McpServer>("/api/v1/platform/ai/mcp-servers", {
        method: "POST",
        body: {
          key: f.key,
          display_name: f.display_name.trim(),
          purpose: f.purpose.trim(),
          owner: f.owner.trim(),
          environment: f.environment,
          endpoint: f.endpoint.trim(),
          allowed_hosts: hosts,
          service_identity: f.service_identity,
          trust_level: f.trust_level,
          vault_ref: f.vault_ref.trim() || null,
          pin: wantsPin ? { server_name: f.pin_name.trim(), version: f.pin_version.trim(), sha256: f.pin_sha.trim() } : null,
        },
      });
      toast({ tone: "success", title: "MCP server registered", description: "It stays disabled until you enable it." });
      onClose();
      router.push(`/platform/ai/tools/servers/${out.id}`);
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(x.fields);
    } finally {
      setBusy(false);
    }
  }

  const field = (k: keyof typeof f, label: string, hint?: string, mono = false) => (
    <Field id={`srv-${k}`} label={label} hint={hint} error={errors[k]}>
      {(p) => <Input {...p} value={f[k]} onChange={set(k)} disabled={busy} className={mono ? "font-mono" : undefined} spellCheck={!mono} />}
    </Field>
  );

  return (
    <Dialog open onClose={onClose} busy={busy} title="Register MCP server" size="xl">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <StepUpHint />
        <div className="grid gap-4 md:grid-cols-2">
          {field("key", "Key", "Stable identifier, e.g. crm-x. Tool ids start with mcp.<key>.", true)}
          {field("display_name", "Display name")}
          {field("owner", "Owner", "Accountable team or person.")}
          {field("environment", "Environment", undefined, true)}
          {field("endpoint", "Endpoint", "Streamable HTTP, https only.", true)}
          <Field id="srv-hosts" label="Allowed hosts" hint="One per line. Requests to any other host are refused." error={errors.allowed_hosts}>
            {(p) => <Textarea {...p} value={f.allowed_hosts} onChange={set("allowed_hosts")} disabled={busy} className="font-mono" rows={2} />}
          </Field>
          {field("service_identity", "Service identity", "Dedicated least-privilege identity for this server.", true)}
          <Field id="srv-trust" label="Trust level" error={errors.trust_level}>
            {(p) => (
              <Select {...p} value={f.trust_level} onChange={set("trust_level")} disabled={busy}>
                {TRUST_LEVELS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {field("vault_ref", "Credential reference", "Optional vault path for this server's own credential. Never a secret value.", true)}
        </div>
        <Field id="srv-purpose" label="Purpose" hint="Why agents need this server." error={errors.purpose}>
          {(p) => <Textarea {...p} value={f.purpose} onChange={set("purpose")} disabled={busy} rows={2} maxLength={2000} />}
        </Field>
        <fieldset className="grid gap-4 rounded-md border border-border p-4 md:grid-cols-3">
          <legend className="px-1 text-sm font-medium">Reviewed pin (optional now, required to enable)</legend>
          <Field id="pin-name" label="Server name" error={errors["pin.server_name"]}>
            {(p) => <Input {...p} value={f.pin_name} onChange={set("pin_name")} disabled={busy} className="font-mono" />}
          </Field>
          <Field id="pin-version" label="Version" error={errors["pin.version"]}>
            {(p) => <Input {...p} value={f.pin_version} onChange={set("pin_version")} disabled={busy} className="font-mono" />}
          </Field>
          <Field id="pin-sha" label="SHA-256" error={errors["pin.sha256"]}>
            {(p) => <Input {...p} value={f.pin_sha} onChange={set("pin_sha")} disabled={busy} className="font-mono" spellCheck={false} />}
          </Field>
        </fieldset>
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "Registering…" : "Register"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

type ToolAction = { tool: ToolDef; action: "disable" | "revoke" };

function Tools() {
  const can = useCan();
  const toast = useToast();
  const tools = useApi<{ items: ToolDef[] }>("/api/v1/platform/ai/tools");
  const [registering, setRegistering] = React.useState(false);
  const [pending, setPending] = React.useState<ToolAction | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [risk, setRisk] = React.useState("");
  const canManage = can("platform:ai:manage");

  async function enable(t: ToolDef) {
    setBusy(t.tool_id);
    try {
      await api(`/api/v1/platform/ai/tools/${encodeURIComponent(t.tool_id)}/enable`, { method: "POST" });
      toast({ tone: "success", title: `${t.tool_id} enabled` });
      tools.reload();
    } catch (err) {
      const x = toApiError(err);
      if (!x.isStepUp) toast({ tone: "error", title: "Could not enable the tool", description: x.message });
    } finally {
      setBusy(null);
    }
  }

  const rows = (tools.data?.items ?? []).filter((t) => risk === "" || String(t.risk_class) === risk);
  const columns: Column<ToolDef>[] = [
    {
      key: "tool",
      header: "Tool",
      cell: (t) => (
        <span>
          <span className="font-medium">{t.display_name}</span>
          <span className="block font-mono text-xs text-muted">
            {t.tool_id} · v{t.version} · {t.kind}
          </span>
        </span>
      ),
    },
    {
      key: "risk",
      header: "Risk class",
      cell: (t) => {
        const r = riskInfo(t.risk_class);
        return (
          <span title={r.help}>
            <Badge tone={r.tone}>{r.label}</Badge>
          </span>
        );
      },
    },
    {
      key: "flags",
      header: "Controls",
      hideOnMobile: true,
      cell: (t) => (
        <span className="flex flex-wrap gap-1">
          {t.side_effecting ? <Badge tone="warn">side effects</Badge> : <Badge>read only</Badge>}
          {t.requires_approval || t.risk_class >= 3 ? <Badge tone="warn">approval</Badge> : null}
          <Badge>{t.data_classification}</Badge>
        </span>
      ),
    },
    {
      key: "state",
      header: "State",
      cell: (t) => {
        const s = toolState(t);
        return (
          <span title={t.revoked_reason ?? t.disabled_reason ?? undefined}>
            <Badge tone={s.tone}>{s.label}</Badge>
          </span>
        );
      },
    },
    { key: "limits", header: "Limits", hideOnMobile: true, cell: (t) => <span className="tabular text-xs">{t.rate_per_minute}/min · {t.timeout_ms} ms</span> },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "text-right",
      cell: (t) =>
        canManage && !t.revoked ? (
          <span className="flex justify-end gap-1">
            {t.enabled ? (
              <Button size="sm" variant="secondary" onClick={() => setPending({ tool: t, action: "disable" })} disabled={busy !== null}>
                Disable
              </Button>
            ) : (
              <Button size="sm" variant="secondary" onClick={() => enable(t)} disabled={busy !== null}>
                {busy === t.tool_id ? "Enabling…" : "Enable"}
              </Button>
            )}
            <Button size="sm" variant="danger" onClick={() => setPending({ tool: t, action: "revoke" })} disabled={busy !== null}>
              Revoke
            </Button>
          </span>
        ) : null,
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Tool catalogue"
        description="Every tool agents can be granted. Internal tools ship through migrations; MCP tools are registered against an approved server."
        actions={
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-2 text-sm">
              <span className="text-muted">Risk</span>
              <Select value={risk} onChange={(e) => setRisk(e.target.value)} className="w-40">
                <option value="">All classes</option>
                {Object.entries(RISK_CLASSES).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v.label}
                  </option>
                ))}
              </Select>
            </label>
            {canManage ? (
              <Button onClick={() => setRegistering(true)}>
                <Plus className="h-4 w-4" aria-hidden /> Register MCP tool
              </Button>
            ) : null}
          </div>
        }
      />
      {tools.loading && !tools.data ? (
        <LoadingState label="Loading tools" />
      ) : tools.error && !tools.data ? (
        <ErrorState problem={tools.error.problem} onRetry={tools.reload} />
      ) : (
        <DataTable caption="Tool catalogue" columns={columns} rows={rows} rowKey={(t) => t.tool_id} empty={<EmptyState title="No tools match" />} />
      )}
      {registering ? <RegisterToolDialog onClose={() => setRegistering(false)} onCreated={tools.reload} /> : null}
      <ConfirmDialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title={pending?.action === "revoke" ? `Revoke ${pending.tool.tool_id}` : `Disable ${pending?.tool.tool_id ?? ""}`}
        description={
          pending?.action === "revoke"
            ? "Emergency revocation for every tenant and agent. It is permanent: a revoked tool cannot be enabled again."
            : "The tool stops for every tenant and agent until it is enabled again."
        }
        confirmLabel={pending?.action === "revoke" ? "Revoke tool" : "Disable tool"}
        typedConfirmation={pending?.action === "revoke" ? pending.tool.tool_id : undefined}
        reason={{ minLength: 5, maxLength: 500 }}
        onConfirm={async (reason) => {
          if (!pending) return;
          await api(`/api/v1/platform/ai/tools/${encodeURIComponent(pending.tool.tool_id)}/${pending.action}`, { method: "POST", body: { reason } });
          toast({ tone: "success", title: pending.action === "revoke" ? "Tool revoked" : "Tool disabled" });
          tools.reload();
        }}
      >
        <StepUpHint />
      </ConfirmDialog>
    </Card>
  );
}

function RegisterToolDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const toast = useToast();
  const servers = useApi<{ items: McpServer[] }>("/api/v1/platform/ai/mcp-servers");
  const usable = (servers.data?.items ?? []).filter((s) => !s.revoked);
  const [f, setF] = React.useState({
    server_id: "",
    tool_id: "",
    tool_name: "",
    display_name: "",
    description: "",
    risk_class: "1",
    side_effecting: false,
    requires_approval: false,
    data_classification: "confidential",
    input_schema: '{\n  "type": "object",\n  "properties": {},\n  "additionalProperties": false\n}',
  });
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const server = usable.find((s) => s.id === f.server_id);
  const prefix = server ? `mcp.${server.key.replace(/-/g, "_")}.` : "mcp.";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const local: Record<string, string> = {};
    if (!f.server_id) local.server_id = "Choose the server that provides this tool.";
    if (!/^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/.test(f.tool_id) || f.tool_id.length > 80) local.tool_id = "Dotted lowercase id, e.g. mcp.crm_x.lookup_contact.";
    else if (server && !f.tool_id.startsWith(prefix)) local.tool_id = `MCP tool ids must start with ${prefix}`;
    if (!/^[A-Za-z0-9_.-]{1,128}$/.test(f.tool_name)) local.tool_name = "The tool's name on the MCP server.";
    if (f.display_name.trim().length < 2) local.display_name = "At least 2 characters.";
    if (f.description.trim().length < 5) local.description = "At least 5 characters.";
    let schema: unknown = null;
    try {
      schema = JSON.parse(f.input_schema);
      if (!schema || typeof schema !== "object" || Array.isArray(schema)) local.input_schema = "A JSON Schema object.";
    } catch {
      local.input_schema = "Not valid JSON.";
    }
    setErrors(local);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/platform/ai/tools", {
        method: "POST",
        body: {
          server_id: f.server_id,
          tool_id: f.tool_id,
          tool_name: f.tool_name,
          display_name: f.display_name.trim(),
          description: f.description.trim(),
          risk_class: Number(f.risk_class),
          side_effecting: f.side_effecting,
          requires_approval: f.requires_approval,
          data_classification: f.data_classification,
          input_schema: schema,
        },
      });
      toast({ tone: "success", title: "Tool registered" });
      onCreated();
      onClose();
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(x.fields);
    } finally {
      setBusy(false);
    }
  }

  const risk = Number(f.risk_class);
  return (
    <Dialog open onClose={onClose} busy={busy} title="Register MCP tool" size="xl">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <StepUpHint />
        <div className="grid gap-4 md:grid-cols-2">
          <Field id="tool-server" label="Server" error={errors.server_id}>
            {(p) => (
              <Select {...p} value={f.server_id} onChange={(e) => setF((s) => ({ ...s, server_id: e.target.value }))} disabled={busy}>
                <option value="">Choose…</option>
                {usable.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.display_name} ({s.key})
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field id="tool-id" label="Tool id" hint={`Must start with ${prefix}`} error={errors.tool_id}>
            {(p) => <Input {...p} value={f.tool_id} placeholder={`${prefix}lookup`} onChange={(e) => setF((s) => ({ ...s, tool_id: e.target.value }))} disabled={busy} className="font-mono" />}
          </Field>
          <Field id="tool-name" label="Name on the server" error={errors.tool_name}>
            {(p) => <Input {...p} value={f.tool_name} onChange={(e) => setF((s) => ({ ...s, tool_name: e.target.value }))} disabled={busy} className="font-mono" />}
          </Field>
          <Field id="tool-display" label="Display name" error={errors.display_name}>
            {(p) => <Input {...p} value={f.display_name} onChange={(e) => setF((s) => ({ ...s, display_name: e.target.value }))} disabled={busy} />}
          </Field>
          <Field id="tool-risk" label="Risk class" hint={riskInfo(risk).help} error={errors.risk_class}>
            {(p) => (
              <Select {...p} value={f.risk_class} onChange={(e) => setF((s) => ({ ...s, risk_class: e.target.value }))} disabled={busy}>
                {Object.entries(RISK_CLASSES).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v.label}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field id="tool-class" label="Data classification" error={errors.data_classification}>
            {(p) => (
              <Select {...p} value={f.data_classification} onChange={(e) => setF((s) => ({ ...s, data_classification: e.target.value }))} disabled={busy}>
                {CLASSIFICATIONS.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        </div>
        <Field id="tool-desc" label="Description" hint="Shown to models; keep it factual." error={errors.description}>
          {(p) => <Textarea {...p} value={f.description} onChange={(e) => setF((s) => ({ ...s, description: e.target.value }))} disabled={busy} rows={2} maxLength={1000} />}
        </Field>
        <div className="flex flex-wrap gap-6">
          <Checkbox label="Has side effects" checked={f.side_effecting} onChange={(e) => setF((s) => ({ ...s, side_effecting: e.target.checked }))} disabled={busy} />
          <Checkbox
            label="Requires human approval"
            description={risk >= 3 ? "Always required for class 3 and above." : undefined}
            checked={f.requires_approval || risk >= 3}
            disabled={busy || risk >= 3}
            onChange={(e) => setF((s) => ({ ...s, requires_approval: e.target.checked }))}
          />
        </div>
        <Field id="tool-schema" label="Input schema (JSON Schema)" hint="Checked by the registry for unsafe constructs." error={errors.input_schema}>
          {(p) => <Textarea {...p} value={f.input_schema} onChange={(e) => setF((s) => ({ ...s, input_schema: e.target.value }))} disabled={busy} rows={6} className="font-mono text-xs" spellCheck={false} />}
        </Field>
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy ? "Registering…" : "Register tool"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

