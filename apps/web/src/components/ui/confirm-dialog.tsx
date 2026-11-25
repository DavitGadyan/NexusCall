"use client";
/**
 * Confirmation for destructive or high-risk actions. Optionally requires typing an exact phrase and/or a
 * reason (sent to the API and recorded in the audit log).
 */
import * as React from "react";
import { type ApiError, toApiError } from "@/lib/client/api";
import { Dialog } from "./dialog";
import { Field, FormError, Textarea } from "./field";
import { Button, Input } from "./primitives";

export type ConfirmDialogProps = {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: React.ReactNode;
  confirmLabel: string;
  tone?: "danger" | "primary";
  /** exact text the user must type, e.g. the tenant slug */
  typedConfirmation?: string;
  reason?: { label?: string; minLength: number; maxLength?: number };
  onConfirm: (reason: string) => Promise<void>;
  children?: React.ReactNode;
};

export function ConfirmDialog({
  open,
  onClose,
  title,
  description,
  confirmLabel,
  tone = "danger",
  typedConfirmation,
  reason,
  onConfirm,
  children,
}: ConfirmDialogProps) {
  const id = React.useId();
  const [typed, setTyped] = React.useState("");
  const [why, setWhy] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);

  React.useEffect(() => {
    if (open) {
      setTyped("");
      setWhy("");
      setError(null);
    }
  }, [open]);

  const typedOk = !typedConfirmation || typed === typedConfirmation;
  const reasonOk = !reason || why.trim().length >= reason.minLength;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!typedOk || !reasonOk) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(why.trim());
      onClose();
    } catch (err) {
      const apiErr = toApiError(err);
      // A step-up prompt is shown globally; keep the dialog open so the user can retry afterwards.
      setError(apiErr);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title={title} description={description} busy={busy} size="md">
      <form id={`${id}-form`} onSubmit={submit} className="flex flex-col gap-4">
        {children}
        {reason ? (
          <Field
            id={`${id}-reason`}
            label={reason.label ?? "Reason (recorded in the audit log)"}
            hint={`At least ${reason.minLength} characters.`}
            error={error?.fields.reason}
            required
          >
            {(p) => <Textarea {...p} value={why} maxLength={reason.maxLength ?? 500} onChange={(e) => setWhy(e.target.value)} />}
          </Field>
        ) : null}
        {typedConfirmation ? (
          <Field
            id={`${id}-typed`}
            label={
              <>
                Type <span className="font-mono font-semibold">{typedConfirmation}</span> to confirm
              </>
            }
          >
            {(p) => <Input {...p} autoComplete="off" spellCheck={false} value={typed} onChange={(e) => setTyped(e.target.value)} />}
          </Field>
        ) : null}
        <FormError message={error && !error.isStepUp ? error.message : null} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant={tone === "danger" ? "danger" : "primary"} disabled={busy || !typedOk || !reasonOk}>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
