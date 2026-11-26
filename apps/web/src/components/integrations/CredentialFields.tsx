"use client";
/**
 * Write-only credential inputs. Values live only in the parent's form state; nothing is pre-filled from the
 * API (it never returns a credential), browsers are asked not to store them, and the parent clears the
 * state after submitting.
 */
import * as React from "react";
import { Field, Input } from "@/components/ui";
import { FIELD_HINTS, fieldLabel, isIdentifierField } from "./lib/connections";
import type { Provider } from "./lib/types";

export function CredentialFields({
  spec,
  values,
  onChange,
  errors,
  disabled,
  idPrefix,
}: {
  spec: Pick<Provider, "credential_fields" | "optional_fields">;
  values: Record<string, string>;
  onChange: (name: string, value: string) => void;
  errors: Record<string, string>;
  disabled?: boolean;
  idPrefix: string;
}) {
  const fields = [...spec.credential_fields.map((n) => ({ name: n, required: true })), ...spec.optional_fields.map((n) => ({ name: n, required: false }))];
  return (
    <fieldset className="flex flex-col gap-4" data-testid="credential-fields">
      <legend className="sr-only">Credentials</legend>
      {fields.map(({ name, required }) => (
        <Field
          key={name}
          id={`${idPrefix}-${name}`}
          label={fieldLabel(name)}
          required={required}
          hint={[FIELD_HINTS[name], required ? null : "Optional."].filter(Boolean).join(" ") || undefined}
          error={errors[`credentials.${name}`]}
        >
          {(p) => (
            <Input
              {...p}
              name={`cred-${name}`}
              type={isIdentifierField(name) ? "text" : "password"}
              autoComplete={isIdentifierField(name) ? "off" : "new-password"}
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              data-1p-ignore
              data-lpignore="true"
              value={values[name] ?? ""}
              disabled={disabled}
              onChange={(e) => onChange(name, e.target.value)}
              className="font-mono"
            />
          )}
        </Field>
      ))}
    </fieldset>
  );
}

export function ConfigFields({
  keys,
  values,
  onChange,
  errors,
  disabled,
  idPrefix,
}: {
  keys: string[];
  values: Record<string, string>;
  onChange: (name: string, value: string) => void;
  errors: Record<string, string>;
  disabled?: boolean;
  idPrefix: string;
}) {
  if (keys.length === 0) return null;
  return (
    <fieldset className="flex flex-col gap-4">
      <legend className="mb-1 text-sm font-medium">Settings</legend>
      {keys.map((k) => (
        <Field key={k} id={`${idPrefix}-cfg-${k}`} label={fieldLabel(k)} hint="Optional. Letters, digits, @ . _ - only." error={errors[`config.${k}`]}>
          {(p) => <Input {...p} value={values[k] ?? ""} maxLength={255} disabled={disabled} onChange={(e) => onChange(k, e.target.value)} />}
        </Field>
      ))}
    </fieldset>
  );
}
