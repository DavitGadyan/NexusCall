"use client";
/**
 * Versioned settings editor shared by tenant settings (`/api/v1/settings`) and platform settings
 * (`/api/v1/platform/settings`). Shows 422 field errors, platform-bound violations, 409 conflicts and
 * locked controls; high-risk sections need a recent step-up and, where configured, a typed confirmation.
 */
import { ShieldCheck } from "lucide-react";
import * as React from "react";
import { api, type ApiError, stepUpLoginUrl, toApiError } from "@/lib/client/api";
import { useApi } from "@/lib/client/use-api";
import { DAYS, errorsByField, type FieldDescriptor, parseList, type SectionDescriptor, sectionPayload } from "@/lib/settings-descriptors";
import type { SettingsSection } from "@/lib/types";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  Checkbox,
  ConfirmDialog,
  ConflictNotice,
  ErrorState,
  Field,
  FormError,
  Input,
  LoadingState,
  LockedBadge,
  Notice,
  Select,
  Tabs,
  useRecentStepUp,
  useToast,
} from "@/components/ui";

type AllSections = Record<string, SettingsSection>;

export function SettingsEditor({
  endpoint,
  sections,
  readOnly = false,
  initialTab,
  extraTabs = [],
}: {
  endpoint: "/api/v1/settings" | "/api/v1/platform/settings";
  sections: SectionDescriptor[];
  readOnly?: boolean;
  initialTab?: string;
  extraTabs?: { id: string; label: string; content: React.ReactNode }[];
}) {
  const all = useApi<AllSections>(endpoint);
  const [tab, setTab] = React.useState(initialTab && [...sections.map((s) => s.id), ...extraTabs.map((t) => t.id)].includes(initialTab) ? initialTab : (sections[0]?.id ?? ""));
  const active = sections.find((s) => s.id === tab);
  const extra = extraTabs.find((t) => t.id === tab);

  return (
    <Tabs
      label="Settings sections"
      value={tab}
      onChange={setTab}
      tabs={[
        ...sections.map((s) => ({ id: s.id, label: s.title, badge: s.highRisk ? <span className="sr-only">(high risk)</span> : null })),
        ...extraTabs.map((t) => ({ id: t.id, label: t.label })),
      ]}
    >
      {extra ? (
        extra.content
      ) : all.loading && !all.data ? (
        <Card>
          <LoadingState label="Loading settings" />
        </Card>
      ) : all.error && !all.data ? (
        <Card>
          <ErrorState problem={all.error.problem} onRetry={all.reload} />
        </Card>
      ) : active && all.data?.[active.id] ? (
        <SectionForm
          key={`${active.id}:${all.data[active.id]?._version ?? 0}`}
          putUrl={`${endpoint}/${active.id}`}
          section={active}
          data={all.data[active.id] as SettingsSection}
          readOnly={readOnly}
          onSaved={(saved) => all.setData((prev) => (prev ? { ...prev, [active.id]: saved } : prev))}
          onReload={all.reload}
        />
      ) : null}
    </Tabs>
  );
}

function SectionForm({
  putUrl,
  section,
  data,
  readOnly,
  onSaved,
  onReload,
}: {
  putUrl: string;
  section: SectionDescriptor;
  data: SettingsSection;
  readOnly: boolean;
  onSaved: (s: SettingsSection) => void;
  onReload: () => void;
}) {
  const toast = useToast();
  const recentStepUp = useRecentStepUp();
  const [values, setValues] = React.useState<Record<string, unknown>>(() => ({ ...data }));
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [confirming, setConfirming] = React.useState(false);
  const dirty = JSON.stringify(sectionPayload(values)) !== JSON.stringify(sectionPayload(data));
  const fieldErrs = errorsByField(error?.fields ?? {});
  const needsStepUp = Boolean(section.highRisk) && !recentStepUp;

  const set = (key: string, value: unknown) => setValues((v) => ({ ...v, [key]: value }));

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const saved = await api<SettingsSection>(putUrl, { method: "PUT", body: { data: sectionPayload(values), expected_version: data._version } });
      onSaved(saved);
      toast({ tone: "success", title: `${section.title} saved`, description: `Version ${saved._version}` });
    } catch (err) {
      const e = toApiError(err);
      setError(e);
      throw e;
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (section.confirm) setConfirming(true);
    else void save().catch(() => undefined);
  }

  const formLevel = error && !error.isConflict && !error.isStepUp && Object.keys(error.fields).length === 0 ? error.message : null;

  return (
    <Card>
      <CardHeader
        title={section.title}
        description={section.description}
        actions={
          <div className="flex items-center gap-2">
            {section.highRisk ? <Badge tone="warn">Step-up required</Badge> : null}
            <Badge>v{data._version}</Badge>
          </div>
        }
      />
      <form onSubmit={onSubmit} className="flex flex-col gap-5 px-5 py-4">
        {error?.isConflict ? <ConflictNotice onReload={onReload} /> : null}
        <FormError message={formLevel} />
        {readOnly ? <Notice tone="info">You can view these settings. Changing them needs the manage permission.</Notice> : null}
        <div className="grid gap-5 md:grid-cols-2">
          {section.fields.map((f) => (
            <SettingField key={f.key} field={f} value={values[f.key]} error={fieldErrs[f.key]} disabled={readOnly || busy || Boolean(f.locked)} onChange={(v) => set(f.key, v)} />
          ))}
        </div>
        {!readOnly ? (
          <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-4">
            {needsStepUp ? (
              <span className="mr-auto text-sm text-muted">Saving this section requires a multi-factor sign-in within the last five minutes.</span>
            ) : null}
            <Button type="button" variant="secondary" disabled={!dirty || busy} onClick={() => setValues({ ...data })}>
              Discard changes
            </Button>
            {needsStepUp ? (
              <a href={stepUpLoginUrl()} className="inline-flex h-9 items-center gap-2 rounded-md bg-brand px-4 text-sm font-medium text-brand-fg hover:bg-brand/90">
                <ShieldCheck className="h-4 w-4" aria-hidden /> Verify to save
              </a>
            ) : (
              <Button type="submit" disabled={!dirty || busy}>
                {busy ? "Saving…" : "Save changes"}
              </Button>
            )}
          </div>
        ) : null}
      </form>
      {section.confirm ? (
        <ConfirmDialog
          open={confirming}
          onClose={() => setConfirming(false)}
          title={`Save ${section.title.toLowerCase()}`}
          description={section.confirm.message}
          confirmLabel="Save"
          typedConfirmation={section.confirm.phrase}
          onConfirm={save}
        />
      ) : null}
    </Card>
  );
}

function SettingField({
  field,
  value,
  error,
  disabled,
  onChange,
}: {
  field: FieldDescriptor;
  value: unknown;
  error?: string;
  disabled: boolean;
  onChange: (v: unknown) => void;
}) {
  const id = `setting-${field.key}`;
  const locked = field.locked ? <LockedBadge reason={field.locked} /> : undefined;
  const hint = field.locked ?? field.hint;

  if (field.kind === "boolean") {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-2">
          <Checkbox id={id} label={field.label} checked={Boolean(value)} disabled={disabled} onChange={(e) => onChange(e.target.checked)} aria-describedby={hint ? `${id}-hint` : undefined} />
          {locked}
        </div>
        {hint ? (
          <p id={`${id}-hint`} className="pl-6 text-xs text-muted">
            {hint}
          </p>
        ) : null}
        {error ? <p className="pl-6 text-xs text-danger">{error}</p> : null}
      </div>
    );
  }

  if (field.kind === "multi") {
    const selected = new Set(Array.isArray(value) ? (value as string[]) : []);
    return (
      <fieldset className="flex flex-col gap-2 md:col-span-2">
        <legend className="mb-1 flex items-center gap-2 text-sm font-medium">
          {field.label} {locked}
        </legend>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {field.options.map((o) => (
            <Checkbox
              key={o.value}
              label={o.label}
              checked={selected.has(o.value)}
              disabled={disabled}
              onChange={(e) => {
                const next = new Set(selected);
                if (e.target.checked) next.add(o.value);
                else next.delete(o.value);
                onChange(field.options.map((x) => x.value).filter((v) => next.has(v)));
              }}
            />
          ))}
        </div>
        {hint ? <p className="text-xs text-muted">{hint}</p> : null}
        {error ? <p className="text-xs text-danger">{error}</p> : null}
      </fieldset>
    );
  }

  if (field.kind === "window") {
    const w = (value && typeof value === "object" ? value : {}) as { start?: string; end?: string; days?: string[] };
    const days = new Set(w.days ?? []);
    return (
      <fieldset className="flex flex-col gap-2 md:col-span-2">
        <legend className="mb-1 text-sm font-medium">{field.label}</legend>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-muted">
            From
            <Input type="time" value={w.start ?? ""} disabled={disabled} onChange={(e) => onChange({ ...w, start: e.target.value })} className="w-32" />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted">
            To
            <Input type="time" value={w.end ?? ""} disabled={disabled} onChange={(e) => onChange({ ...w, end: e.target.value })} className="w-32" />
          </label>
          <div className="flex flex-wrap gap-3" role="group" aria-label={`${field.label} days`}>
            {DAYS.map((d) => (
              <Checkbox
                key={d.value}
                label={d.label}
                checked={days.has(d.value)}
                disabled={disabled}
                onChange={(e) => {
                  const next = new Set(days);
                  if (e.target.checked) next.add(d.value);
                  else next.delete(d.value);
                  onChange({ ...w, days: DAYS.map((x) => x.value).filter((v) => next.has(v)) });
                }}
              />
            ))}
          </div>
        </div>
        {hint ? <p className="text-xs text-muted">{hint}</p> : null}
        {error ? <p className="text-xs text-danger">{error}</p> : null}
      </fieldset>
    );
  }

  return (
    <Field id={id} label={field.label} hint={hint} error={error} locked={locked}>
      {(p) => {
        if (field.kind === "select") {
          return (
            <Select {...p} value={String(value ?? "")} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
              {field.options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </Select>
          );
        }
        if (field.kind === "number") {
          return (
            <div className="flex items-center gap-2">
              <Input
                {...p}
                type="number"
                inputMode="numeric"
                min={field.min}
                max={field.max}
                value={value === null || value === undefined ? "" : String(value)}
                disabled={disabled}
                onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
                className="tabular"
              />
              {field.suffix ? <span className="shrink-0 text-xs text-muted">{field.suffix}</span> : null}
            </div>
          );
        }
        if (field.kind === "list") {
          return (
            <Input
              {...p}
              placeholder={field.placeholder}
              defaultValue={Array.isArray(value) ? (value as string[]).join(", ") : ""}
              disabled={disabled}
              onBlur={(e) => onChange(parseList(e.target.value, field.upper))}
            />
          );
        }
        if (field.kind === "color") {
          const v = typeof value === "string" ? value : "#4F46E5";
          return (
            <div className="flex items-center gap-2">
              <input type="color" aria-label={`${field.label} picker`} value={/^#[0-9a-fA-F]{6}$/.test(v) ? v : "#4F46E5"} disabled={disabled} onChange={(e) => onChange(e.target.value.toUpperCase())} className="h-9 w-12 rounded border border-border bg-surface" />
              <Input {...p} value={v} maxLength={7} disabled={disabled} onChange={(e) => onChange(e.target.value)} className="w-32 font-mono" />
            </div>
          );
        }
        if (field.kind === "timezone") {
          return <TimezoneInput {...p} value={String(value ?? "")} disabled={disabled} onChange={onChange} />;
        }
        const placeholder = field.kind === "text" || field.kind === "url" ? field.placeholder : undefined;
        return (
          <Input
            {...p}
            type={field.kind === "url" ? "url" : "text"}
            placeholder={placeholder}
            maxLength={field.kind === "text" ? field.maxLength : 500}
            value={value === null || value === undefined ? "" : String(value)}
            disabled={disabled}
            onChange={(e) => onChange(field.kind === "url" && e.target.value === "" ? null : e.target.value)}
          />
        );
      }}
    </Field>
  );
}

function TimezoneInput({ value, disabled, onChange, ...p }: { id: string; value: string; disabled: boolean; onChange: (v: string) => void }) {
  const zones = React.useMemo(() => {
    const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] };
    return intl.supportedValuesOf ? intl.supportedValuesOf("timeZone") : [];
  }, []);
  const listId = `${p.id}-zones`;
  return (
    <>
      <Input {...p} list={listId} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} placeholder="America/New_York" />
      <datalist id={listId}>
        {["UTC", ...zones].map((z) => (
          <option key={z} value={z} />
        ))}
      </datalist>
    </>
  );
}
