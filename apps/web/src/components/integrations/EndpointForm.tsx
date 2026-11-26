"use client";
/** Form fields for a webhook endpoint. The signing secret input is write-only and empty on every open. */
import { Wand2 } from "lucide-react";
import * as React from "react";
import { Button, Checkbox, Field, Input } from "@/components/ui";
import { EVENT_TYPES, type EndpointForm, generateSecret, SECRET_MAX, SECRET_MIN } from "./lib/webhooks";

export function EndpointFields({
  value,
  onChange,
  errors,
  disabled,
  withSecret,
}: {
  value: EndpointForm;
  onChange: (next: EndpointForm) => void;
  errors: Record<string, string>;
  disabled?: boolean;
  withSecret: boolean;
}) {
  const set = <K extends keyof EndpointForm>(k: K, v: EndpointForm[K]) => onChange({ ...value, [k]: v });
  const selected = new Set(value.event_types);
  return (
    <div className="flex flex-col gap-4">
      <Field id="wh-url" label="Receiver URL" required hint="HTTPS only. Private and internal addresses are refused." error={errors.url}>
        {(p) => <Input {...p} type="url" inputMode="url" placeholder="https://hooks.example.com/nexuscall" value={value.url} maxLength={2048} disabled={disabled} onChange={(e) => set("url", e.target.value)} />}
      </Field>
      <Field id="wh-desc" label="Description" hint="Optional." error={errors.description}>
        {(p) => <Input {...p} value={value.description} maxLength={200} disabled={disabled} onChange={(e) => set("description", e.target.value)} />}
      </Field>
      <fieldset className="flex flex-col gap-2" aria-describedby={errors.event_types ? "wh-events-error" : undefined}>
        <legend className="mb-1 text-sm font-medium">
          Events<span className="text-danger" aria-hidden> *</span>
        </legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {EVENT_TYPES.map((e) => (
            <Checkbox
              key={e.value}
              label={e.label}
              description={<span className="font-mono">{e.value}</span>}
              checked={selected.has(e.value)}
              disabled={disabled}
              onChange={(ev) => {
                const next = new Set(selected);
                if (ev.target.checked) next.add(e.value);
                else next.delete(e.value);
                set("event_types", EVENT_TYPES.map((x) => x.value).filter((v) => next.has(v)));
              }}
            />
          ))}
        </div>
        {errors.event_types ? (
          <p id="wh-events-error" className="text-xs text-danger">
            {errors.event_types}
          </p>
        ) : null}
      </fieldset>
      {withSecret ? <SecretField value={value.secret} onChange={(v) => set("secret", v)} error={errors.secret} disabled={disabled} /> : null}
    </div>
  );
}

export function SecretField({ value, onChange, error, disabled, id = "wh-secret" }: { value: string; onChange: (v: string) => void; error?: string; disabled?: boolean; id?: string }) {
  return (
    <Field
      id={id}
      label="Signing secret"
      required
      hint={`${SECRET_MIN}–${SECRET_MAX} characters. Configure the same value in your receiver to verify signatures; it cannot be viewed after saving.`}
      error={error}
    >
      {(p) => (
        <div className="flex gap-2">
          <Input
            {...p}
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            data-1p-ignore
            data-lpignore="true"
            value={value}
            maxLength={SECRET_MAX}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
            className="font-mono"
          />
          <Button
            type="button"
            variant="secondary"
            disabled={disabled}
            onClick={async () => {
              const s = generateSecret();
              onChange(s);
              try {
                await navigator.clipboard.writeText(s);
              } catch {
                /* clipboard unavailable: the user can still paste their own secret */
              }
            }}
            title="Generate a random secret and copy it to the clipboard"
          >
            <Wand2 className="h-4 w-4" aria-hidden /> Generate &amp; copy
          </Button>
        </div>
      )}
    </Field>
  );
}
