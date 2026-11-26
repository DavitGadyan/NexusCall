"use client";
/** Create or edit a contact. Edits send only changed fields with `expected_version`; a 409 offers a reload. */
import * as React from "react";
import { api, type ApiError, toApiError } from "@/lib/client/api";
import { Button, Dialog, Field, Input, Select } from "@/components/ui";
import { MutationError } from "./common";
import { buildContact, type ContactForm, contactPatch, contactToForm, emptyContactForm } from "./lib/contact-form";
import { newIdempotencyKey } from "./lib/http";
import type { Contact, FieldDef } from "./lib/types";

export function ContactDialog({
  open,
  onClose,
  contact,
  fields,
  onSaved,
  onReload,
}: {
  open: boolean;
  onClose: () => void;
  /** null creates a new contact */
  contact: Contact | null;
  fields: FieldDef[];
  onSaved: (c: Contact) => void;
  onReload?: () => void;
}) {
  const [form, setForm] = React.useState<ContactForm>(emptyContactForm);
  const [localErrors, setLocalErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<ApiError | null>(null);
  const [busy, setBusy] = React.useState(false);
  const idemKey = React.useRef<string>("");

  React.useEffect(() => {
    if (!open) return;
    setForm(contact ? contactToForm(contact) : emptyContactForm());
    setLocalErrors({});
    setError(null);
    idemKey.current = newIdempotencyKey();
  }, [open, contact]);

  const set = <K extends keyof ContactForm>(k: K, v: ContactForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const err = (k: string) => localErrors[k] ?? error?.fields[k];

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const built = buildContact(form, fields, contact ? "edit" : "create");
    setLocalErrors(built.errors);
    setError(null);
    if (Object.keys(built.errors).length) return;
    setBusy(true);
    try {
      let saved: Contact;
      if (contact) {
        const patch = contactPatch(contact, built);
        if (Object.keys(patch).length === 0) {
          onClose();
          return;
        }
        saved = await api<Contact>(`/api/v1/contacts/${contact.id}`, { method: "PATCH", body: patch });
      } else {
        saved = await api<Contact>("/api/v1/contacts", { method: "POST", body: built.body, idempotencyKey: idemKey.current });
      }
      onSaved(saved);
    } catch (e2) {
      setError(toApiError(e2));
    } finally {
      setBusy(false);
    }
  }

  const text = (key: keyof ContactForm, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <Field id={`contact-${key}`} label={label} error={err(key === "phone" ? "phone" : key)}>
      {(p) => <Input {...p} {...props} value={form[key] as string} onChange={(e) => set(key, e.target.value as never)} />}
    </Field>
  );

  return (
    <Dialog open={open} onClose={onClose} busy={busy} size="lg" title={contact ? "Edit contact" : "New contact"} description="Phone numbers are normalised to E.164 using the country (default US).">
      <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
        <div className="grid gap-3 sm:grid-cols-2">
          {text("first_name", "First name", { autoComplete: "off", maxLength: 100 })}
          {text("last_name", "Last name", { autoComplete: "off", maxLength: 100 })}
          {text("company", "Company", { maxLength: 200 })}
          {text("email", "Email", { type: "email", autoComplete: "off", maxLength: 320 })}
          {text("phone", "Phone", { type: "tel", autoComplete: "off", maxLength: 32 })}
          {text("country", "Country", { placeholder: "US", maxLength: 2 })}
          {text("timezone", "Timezone", { placeholder: "America/New_York", maxLength: 64 })}
          {text("locale", "Locale", { placeholder: "en-US", maxLength: 5 })}
          {text("source", "Source", { placeholder: "website", maxLength: 64 })}
          <Field id="contact-status" label="Status" error={err("status")}>
            {(p) => (
              <Select {...p} value={form.status} onChange={(e) => set("status", e.target.value as ContactForm["status"])}>
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
              </Select>
            )}
          </Field>
        </div>
        {fields.length > 0 ? (
          <fieldset className="flex flex-col gap-3">
            <legend className="mb-1 text-sm font-semibold">Custom fields</legend>
            <div className="grid gap-3 sm:grid-cols-2">
              {fields.map((d) => (
                <Field key={d.id} id={`cf-${d.key}`} label={d.label} required={d.required && !contact} error={err(`custom_fields.${d.key}`)}>
                  {(p) =>
                    d.type === "select" || d.type === "boolean" ? (
                      <Select {...p} value={form.custom[d.key] ?? ""} onChange={(e) => set("custom", { ...form.custom, [d.key]: e.target.value })}>
                        <option value="">Not set</option>
                        {(d.type === "boolean" ? ["true", "false"] : d.options).map((o) => (
                          <option key={o} value={o}>
                            {d.type === "boolean" ? (o === "true" ? "Yes" : "No") : o}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <Input
                        {...p}
                        type={d.type === "number" ? "number" : d.type === "date" ? "date" : "text"}
                        value={form.custom[d.key] ?? ""}
                        onChange={(e) => set("custom", { ...form.custom, [d.key]: e.target.value })}
                      />
                    )
                  }
                </Field>
              ))}
            </div>
          </fieldset>
        ) : null}
        {localErrors._form ? <p role="alert" className="text-sm text-danger">{localErrors._form}</p> : null}
        <MutationError error={error} onReload={onReload ? () => { onClose(); onReload(); } : undefined} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy} data-testid="save-contact">
            {busy ? "Saving…" : contact ? "Save changes" : "Create contact"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
