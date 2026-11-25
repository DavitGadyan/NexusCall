/** Form controls with labels, hints and error messages wired for assistive technology. */
import * as React from "react";
import { cn } from "@/lib/cn";

export type FieldControlProps = { id: string; "aria-invalid"?: boolean; "aria-describedby"?: string };

export function Field({
  id,
  label,
  hint,
  error,
  required,
  locked,
  children,
  className,
}: {
  id: string;
  label: React.ReactNode;
  hint?: React.ReactNode;
  error?: string | null;
  required?: boolean;
  locked?: React.ReactNode;
  children: (props: FieldControlProps) => React.ReactNode;
  className?: string;
}) {
  const described = [hint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="text-sm font-medium">
          {label}
          {required ? <span className="text-danger" aria-hidden> *</span> : null}
        </label>
        {locked}
      </div>
      {children({ id, "aria-invalid": error ? true : undefined, "aria-describedby": described })}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

const control =
  "w-full rounded-md border border-border bg-surface px-3 text-sm placeholder:text-muted focus:border-brand focus:outline-none disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-muted aria-[invalid=true]:border-danger";

export const Select = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(function Select(
  { className, ...props },
  ref,
) {
  return <select ref={ref} className={cn(control, "h-9 pr-8", className)} {...props} />;
});

export const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...props }, ref) {
    return <textarea ref={ref} className={cn(control, "min-h-[80px] py-2", className)} {...props} />;
  },
);

export function Checkbox({
  label,
  description,
  className,
  ...props
}: Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> & { label: React.ReactNode; description?: React.ReactNode }) {
  const autoId = React.useId();
  const id = props.id ?? autoId;
  return (
    <div className={cn("flex items-start gap-2", className)}>
      <input id={id} type="checkbox" className="mt-0.5 h-4 w-4 rounded border-border accent-[rgb(var(--brand))] disabled:opacity-60" {...props} />
      <label htmlFor={id} className={cn("text-sm", props.disabled && "text-muted")}>
        <span>{label}</span>
        {description ? <span className="block text-xs text-muted">{description}</span> : null}
      </label>
    </div>
  );
}

export function FormError({ message }: { message?: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
      {message}
    </p>
  );
}
