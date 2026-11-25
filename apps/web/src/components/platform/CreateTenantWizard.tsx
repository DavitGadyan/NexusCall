"use client";
/**
 * Create-tenant wizard (build.md §22.1): details → owner → review. Creating the tenant also creates its
 * default settings and the owner's invitation in one audited operation; it needs step-up MFA.
 */
import Link from "next/link";
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { slugify } from "@/lib/format";
import type { Invitation } from "@/lib/types";
import { Button, Card, Field, FormError, Input, KeyValues, Notice, StatusBadge, StepUpGate } from "@/components/ui";

type Draft = { name: string; slug: string; timezone: string; data_region: string; owner_email: string };
type Created = { id: string; slug: string; name: string; owner_invitation: Invitation };

const STEPS = ["Details", "Owner", "Review"] as const;
const SLUG = /^[a-z0-9][a-z0-9-]{1,62}$/;
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export function CreateTenantWizard() {
  const [step, setStep] = React.useState(0);
  const [draft, setDraft] = React.useState<Draft>({ name: "", slug: "", timezone: "UTC", data_region: "default", owner_email: "" });
  const [slugTouched, setSlugTouched] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [created, setCreated] = React.useState<Created | null>(null);
  const idempotency = React.useRef<string>(crypto.randomUUID());

  const set = (k: keyof Draft, v: string) =>
    setDraft((d) => ({ ...d, [k]: v, ...(k === "name" && !slugTouched ? { slug: slugify(v) } : {}) }));

  const detailsOk = draft.name.trim().length >= 2 && SLUG.test(draft.slug) && draft.timezone.length > 0 && /^[a-z0-9-]{2,32}$/.test(draft.data_region);
  const ownerOk = EMAIL.test(draft.owner_email.trim());

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const out = await api<Created>("/api/v1/platform/tenants", {
        method: "POST",
        body: { ...draft, name: draft.name.trim(), owner_email: draft.owner_email.trim() },
        idempotencyKey: idempotency.current,
      });
      setCreated(out);
    } catch (err) {
      const e = toApiError(err);
      setError(e);
      if (e.fields.slug || e.isConflict) setStep(0);
      else if (e.fields.owner_email) setStep(1);
    } finally {
      setBusy(false);
    }
  }

  if (created) {
    const inv = created.owner_invitation;
    return (
      <Card className="p-5" data-testid="tenant-created">
        <Notice tone="ok" title={`${created.name} was created`}>
          Default settings were applied and the owner invitation was issued.
        </Notice>
        <div className="mt-4">
          <KeyValues
            items={[
              { label: "Slug", value: <span className="font-mono">{created.slug}</span> },
              { label: "Owner", value: inv.email },
              { label: "Invitation", value: <StatusBadge status={inv.status} /> },
              ...(inv.delivery_error ? [{ label: "Delivery", value: <span className="text-danger">{inv.delivery_error}: resend it from the tenant page.</span> }] : []),
            ]}
          />
        </div>
        <div className="mt-5 flex gap-2">
          <Link className="inline-flex h-9 items-center rounded-md bg-brand px-4 text-sm font-medium text-brand-fg" href={`/platform/tenants/${created.id}`}>
            Open tenant
          </Link>
          <Link className="inline-flex h-9 items-center rounded-md border border-border px-4 text-sm" href="/platform/tenants">
            Back to directory
          </Link>
        </div>
      </Card>
    );
  }

  return (
    <Card className="p-5">
      <ol className="mb-6 flex flex-wrap gap-2 text-sm" aria-label="Steps">
        {STEPS.map((s, i) => (
          <li key={s} aria-current={i === step ? "step" : undefined} className={i === step ? "rounded-full bg-brand/10 px-3 py-1 font-medium text-brand" : "rounded-full px-3 py-1 text-muted"}>
            {i + 1}. {s}
          </li>
        ))}
      </ol>
      {error?.isConflict ? <Notice tone="warn" title="Slug already in use">Choose a different slug.</Notice> : <FormError message={error && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null} />}

      {step === 0 ? (
        <form
          className="grid gap-4 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (detailsOk) setStep(1);
          }}
        >
          <Field id="t-name" label="Tenant name" required error={error?.fields.name}>
            {(p) => <Input {...p} value={draft.name} maxLength={200} onChange={(e) => set("name", e.target.value)} data-testid="tenant-name" />}
          </Field>
          <Field id="t-slug" label="Slug" required hint="Lowercase letters, digits and hyphens. Cannot be changed later." error={error?.fields.slug ?? (draft.slug && !SLUG.test(draft.slug) ? "2–63 characters: a–z, 0–9 and -" : undefined)}>
            {(p) => (
              <Input
                {...p}
                value={draft.slug}
                className="font-mono"
                onChange={(e) => {
                  setSlugTouched(true);
                  set("slug", e.target.value.toLowerCase());
                }}
                data-testid="tenant-slug"
              />
            )}
          </Field>
          <Field id="t-tz" label="Timezone" required hint="IANA name, e.g. America/New_York." error={error?.fields.timezone}>
            {(p) => <Input {...p} value={draft.timezone} onChange={(e) => set("timezone", e.target.value)} />}
          </Field>
          <Field id="t-region" label="Data region" hint="Where this tenant's data is stored." error={error?.fields.data_region}>
            {(p) => <Input {...p} value={draft.data_region} onChange={(e) => set("data_region", e.target.value)} />}
          </Field>
          <div className="flex justify-end md:col-span-2">
            <Button type="submit" disabled={!detailsOk}>
              Next
            </Button>
          </div>
        </form>
      ) : null}

      {step === 1 ? (
        <form
          className="flex max-w-lg flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (ownerOk) setStep(2);
          }}
        >
          <Field id="t-owner" label="Owner email" required hint="The owner receives a single-use invitation and must set up MFA before accepting." error={error?.fields.owner_email}>
            {(p) => <Input {...p} type="email" value={draft.owner_email} onChange={(e) => set("owner_email", e.target.value)} data-testid="tenant-owner" />}
          </Field>
          <div className="flex justify-between">
            <Button type="button" variant="secondary" onClick={() => setStep(0)}>
              Back
            </Button>
            <Button type="submit" disabled={!ownerOk}>
              Next
            </Button>
          </div>
        </form>
      ) : null}

      {step === 2 ? (
        <div className="flex flex-col gap-5">
          <KeyValues
            items={[
              { label: "Name", value: draft.name },
              { label: "Slug", value: <span className="font-mono">{draft.slug}</span> },
              { label: "Timezone", value: draft.timezone },
              { label: "Data region", value: draft.data_region },
              { label: "Owner", value: draft.owner_email },
            ]}
          />
          <StepUpGate description="Creating a tenant is a high-risk action. Verify with your authenticator; your entries are kept only on this page, so re-enter them if the page reloads.">
            <div className="flex justify-between">
              <Button type="button" variant="secondary" onClick={() => setStep(1)} disabled={busy}>
                Back
              </Button>
              <Button onClick={create} disabled={busy} data-testid="create-tenant">
                {busy ? "Creating…" : "Create tenant and invite owner"}
              </Button>
            </div>
          </StepUpGate>
        </div>
      ) : null}
    </Card>
  );
}
