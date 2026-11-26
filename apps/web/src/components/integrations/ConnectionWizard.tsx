"use client";
/**
 * New provider connection: choose the provider, enter credentials in a write-only form (or connect with
 * OAuth), let the server test them, then see the resulting status. Creating needs `integrations:manage`
 * and a recent step-up; it is audited. Credentials are cleared from memory as soon as the request is sent.
 */
import { ArrowLeft, ArrowRight, ExternalLink, ShieldCheck } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, stepUpLoginUrl, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { formatDateTime } from "@/lib/format";
import { Badge, Button, Card, CardHeader, ErrorState, Field, FormError, Input, KeyValues, LoadingState, Notice, useRecentStepUp, useToast } from "@/components/ui";
import { useCan } from "@/components/shell/session-context";
import { newIdempotencyKey } from "@/components/crm/lib/http";
import { ConfigFields, CredentialFields } from "./CredentialFields";
import {
  buildConfig,
  buildCredentials,
  credentialFieldErrors,
  healthInfo,
  PROVIDER_PURPOSE,
  providerLabel,
  safeProviderUrl,
  tenantProviders,
} from "./lib/connections";
import type { Connection, Provider } from "./lib/types";

type Step = "provider" | "credentials" | "result";

export function ConnectionWizard({ basePath }: { basePath: string }) {
  const can = useCan();
  const providers = useApi<{ items: Provider[] }>("/api/v1/integrations/providers");
  const [step, setStep] = React.useState<Step>("provider");
  const [provider, setProvider] = React.useState<Provider | null>(null);
  const [created, setCreated] = React.useState<Connection | null>(null);

  if (!can("integrations:manage")) {
    return (
      <Card>
        <ErrorState problem={{ status: 403, detail: "Adding connections needs the integrations:manage permission." }} />
      </Card>
    );
  }

  const steps: { id: Step; label: string }[] = [
    { id: "provider", label: "Provider" },
    { id: "credentials", label: "Credentials" },
    { id: "result", label: "Test & status" },
  ];
  const current = steps.findIndex((s) => s.id === step);

  return (
    <div className="flex flex-col gap-4">
      <ol className="flex flex-wrap gap-2 text-sm" aria-label="Steps">
        {steps.map((s, i) => (
          <li key={s.id} aria-current={i === current ? "step" : undefined} className={i === current ? "font-medium text-fg" : "text-muted"}>
            <span className="tabular mr-1.5 inline-flex h-5 w-5 items-center justify-center rounded-full border border-border text-xs">{i + 1}</span>
            {s.label}
            {i < steps.length - 1 ? <span aria-hidden className="ml-2 text-muted">›</span> : null}
          </li>
        ))}
      </ol>

      {step === "provider" ? (
        <Card>
          <CardHeader title="Choose a provider" description="Credentials are stored in the platform vault; the UI never shows them again." />
          <div className="px-5 py-4">
            {providers.loading && !providers.data ? (
              <LoadingState label="Loading providers" />
            ) : providers.error && !providers.data ? (
              <ErrorState problem={providers.error.problem} onRetry={providers.reload} />
            ) : (
              <ProviderPicker
                items={tenantProviders(providers.data?.items ?? [])}
                value={provider?.provider ?? null}
                onChange={(p) => setProvider(p)}
              />
            )}
          </div>
          <div className="flex justify-end gap-2 border-t border-border px-5 py-3">
            <Link href={basePath} className="inline-flex h-9 items-center rounded-md border border-border px-4 text-sm hover:bg-surface-2">
              Cancel
            </Link>
            <Button disabled={!provider} onClick={() => setStep("credentials")} data-testid="wizard-next">
              Continue <ArrowRight className="h-4 w-4" aria-hidden />
            </Button>
          </div>
        </Card>
      ) : null}

      {step === "credentials" && provider ? (
        <CredentialStep
          key={provider.provider}
          provider={provider}
          onBack={() => setStep("provider")}
          onCreated={(c) => {
            setCreated(c);
            setStep("result");
          }}
        />
      ) : null}

      {step === "result" && created ? (
        <ResultStep
          initial={created}
          basePath={basePath}
          onAnother={() => {
            setCreated(null);
            setProvider(null);
            setStep("provider");
          }}
        />
      ) : null}
    </div>
  );
}

function ProviderPicker({ items, value, onChange }: { items: Provider[]; value: string | null; onChange: (p: Provider) => void }) {
  if (items.length === 0) return <p className="text-sm text-muted">No providers are available for this workspace.</p>;
  return (
    <fieldset>
      <legend className="sr-only">Provider</legend>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((p) => {
          const id = `provider-${p.provider}`;
          const checked = value === p.provider;
          return (
            <label
              key={p.provider}
              htmlFor={id}
              className={`flex cursor-pointer flex-col gap-1 rounded-md border px-4 py-3 text-sm ${checked ? "border-brand bg-brand/5" : "border-border hover:bg-surface-2"}`}
            >
              <span className="flex items-center gap-2">
                <input id={id} type="radio" name="provider" value={p.provider} checked={checked} onChange={() => onChange(p)} className="accent-[rgb(var(--brand))]" />
                <span className="font-medium">{providerLabel(p.provider)}</span>
                {p.oauth ? <Badge tone="brand">OAuth</Badge> : null}
              </span>
              <span className="pl-5 text-xs text-muted">{PROVIDER_PURPOSE[p.provider] ?? p.provider}</span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

function CredentialStep({ provider, onBack, onCreated }: { provider: Provider; onBack: () => void; onCreated: (c: Connection) => void }) {
  const recentStepUp = useRecentStepUp();
  const toast = useToast();
  const [mode, setMode] = React.useState<"oauth" | "manual">(provider.oauth ? "oauth" : "manual");
  const [creds, setCreds] = React.useState<Record<string, string>>({});
  const [config, setConfig] = React.useState<Record<string, string>>({});
  const [displayName, setDisplayName] = React.useState("");
  const [scope, setScope] = React.useState("default");
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const idemKey = React.useRef(newIdempotencyKey());

  // Never keep credentials around after leaving this step.
  React.useEffect(() => () => setCreds({}), []);

  async function startOAuth() {
    setBusy(true);
    setError(null);
    try {
      const out = await api<{ authorization_url: string; expires_at: string }>(`/api/v1/integrations/oauth/${encodeURIComponent(provider.provider)}/start`);
      const url = safeProviderUrl(out.authorization_url);
      if (!url) {
        toast({ tone: "error", title: "The provider sign-in link was rejected", description: "Only secure https links are followed." });
        return;
      }
      window.location.assign(url);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const built = buildCredentials(provider, creds);
    const local: Record<string, string> = { ...built.errors };
    if (!/^[a-z0-9_-]{1,40}$/.test(scope)) local.scope = "Lowercase letters, digits, - and _ (1–40).";
    setErrors(local);
    if (Object.keys(local).length) return;
    setBusy(true);
    setError(null);
    try {
      const c = await api<Connection>("/api/v1/integrations/connections", {
        method: "POST",
        idempotencyKey: idemKey.current,
        body: {
          provider: provider.provider,
          display_name: displayName.trim() || null,
          scope,
          credentials: built.credentials,
          config: buildConfig(provider, config),
        },
      });
      setCreds({});
      onCreated(c);
    } catch (err) {
      const x = toApiError(err);
      setError(x);
      setErrors(credentialFieldErrors(x.problem, x.fields));
      // A rejected request will not be replayed: a corrected form is a new request.
      if (x.status !== 0 && !x.isStepUp) idemKey.current = newIdempotencyKey();
    } finally {
      setBusy(false);
    }
  }

  const stepUpNotice = !recentStepUp ? (
    <Notice
      tone="warn"
      title="Verification needed"
      action={
        <a href={stepUpLoginUrl()} className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-3 text-sm hover:bg-surface-2">
          <ShieldCheck className="h-4 w-4" aria-hidden /> Verify with MFA
        </a>
      }
    >
      Connecting a provider needs a multi-factor sign-in within the last five minutes. Verify first; anything typed here is discarded when you leave.
    </Notice>
  ) : null;

  return (
    <Card>
      <CardHeader
        title={`Connect ${providerLabel(provider.provider)}`}
        description="Write-only: after saving, the values cannot be viewed, only replaced (rotated) or revoked."
        actions={<Badge tone="warn">Needs recent MFA</Badge>}
      />
      <div className="flex flex-col gap-4 px-5 py-4">
        {stepUpNotice}
        {provider.oauth ? (
          <div role="radiogroup" aria-label="How to connect" className="flex flex-wrap gap-2">
            {(["oauth", "manual"] as const).map((m) => (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={mode === m}
                onClick={() => setMode(m)}
                className={`rounded-md border px-3 py-1.5 text-sm ${mode === m ? "border-brand bg-brand/5 font-medium" : "border-border hover:bg-surface-2"}`}
              >
                {m === "oauth" ? "Sign in with the provider (recommended)" : "Enter credentials manually"}
              </button>
            ))}
          </div>
        ) : null}

        {mode === "oauth" ? (
          <div className="flex flex-col items-start gap-3">
            <p className="text-sm text-muted">
              You will be sent to {providerLabel(provider.provider)} to approve access, then brought back here. The platform stores the resulting token; you never see it.
            </p>
            <FormError message={error && !error.isStepUp ? error.message : null} />
            <div className="flex gap-2">
              <Button type="button" variant="secondary" onClick={onBack} disabled={busy}>
                <ArrowLeft className="h-4 w-4" aria-hidden /> Back
              </Button>
              <Button type="button" onClick={startOAuth} disabled={busy} data-testid="oauth-start">
                <ExternalLink className="h-4 w-4" aria-hidden /> {busy ? "Opening…" : `Continue to ${providerLabel(provider.provider)}`}
              </Button>
            </div>
          </div>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-4" autoComplete="off" noValidate>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field id="conn-name" label="Display name" hint="Optional, e.g. “Main Twilio account”." error={errors.display_name}>
                {(p) => <Input {...p} value={displayName} maxLength={120} onChange={(e) => setDisplayName(e.target.value)} />}
              </Field>
              <Field id="conn-scope" label="Label" hint="Distinguishes several connections to one provider." error={errors.scope}>
                {(p) => <Input {...p} value={scope} maxLength={40} onChange={(e) => setScope(e.target.value.toLowerCase())} />}
              </Field>
            </div>
            <CredentialFields spec={provider} values={creds} errors={errors} disabled={busy} idPrefix="new" onChange={(n, v) => setCreds((c) => ({ ...c, [n]: v }))} />
            <ConfigFields keys={provider.config_keys} values={config} errors={errors} disabled={busy} idPrefix="new" onChange={(n, v) => setConfig((c) => ({ ...c, [n]: v }))} />
            <FormError message={error && !error.isStepUp ? (Object.keys(errors).length ? "Some fields need attention." : error.message) : null} />
            <div className="flex justify-between gap-2 border-t border-border pt-4">
              <Button type="button" variant="secondary" onClick={onBack} disabled={busy}>
                <ArrowLeft className="h-4 w-4" aria-hidden /> Back
              </Button>
              <Button type="submit" disabled={busy} data-testid="save-connection">
                {busy ? "Saving and testing…" : "Save and test"}
              </Button>
            </div>
          </form>
        )}
      </div>
    </Card>
  );
}

function ResultStep({ initial, basePath, onAnother }: { initial: Connection; basePath: string; onAnother: () => void }) {
  const [conn, setConn] = React.useState(initial);
  const [testing, setTesting] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const ran = React.useRef(false);

  const runTest = React.useCallback(async () => {
    setTesting(true);
    setError(null);
    try {
      setConn(await api<Connection>(`/api/v1/integrations/connections/${initial.id}/test`, { method: "POST" }));
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setTesting(false);
    }
  }, [initial.id]);

  React.useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    void runTest();
  }, [runTest]);

  const h = healthInfo(conn.health);
  return (
    <Card>
      <CardHeader
        title={`${providerLabel(conn.provider)} saved`}
        description="The server tested the stored credentials against the provider."
        actions={<Badge tone={testing ? "neutral" : h.tone}>{testing ? "Testing…" : h.label}</Badge>}
      />
      <div className="flex flex-col gap-4 px-5 py-4" aria-live="polite">
        {conn.last_error && !testing ? <Notice tone="danger" title="Test failed">{conn.last_error}</Notice> : null}
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <KeyValues
          items={[
            { label: "Status", value: conn.status },
            { label: "Account", value: conn.account_masked ?? "—" },
            { label: "Last validated", value: formatDateTime(conn.last_validated_at) },
            { label: "Rotation due", value: formatDateTime(conn.next_rotation_due) },
          ]}
        />
      </div>
      <div className="flex flex-wrap justify-end gap-2 border-t border-border px-5 py-3">
        <Button variant="secondary" onClick={() => void runTest()} disabled={testing}>
          Test again
        </Button>
        <Button variant="secondary" onClick={onAnother}>
          Add another
        </Button>
        <Link href={`${basePath}/${conn.id}`} className="inline-flex h-9 items-center rounded-md bg-brand px-4 text-sm font-medium text-brand-fg hover:bg-brand/90" data-testid="open-connection">
          Open connection
        </Link>
      </div>
    </Card>
  );
}
