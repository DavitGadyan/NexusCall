"use client";
/**
 * Modal dialog on the native <dialog> element: top layer, inert background, focus containment and Esc
 * handling come from the browser. Focus returns to the opener on close.
 */
import { X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

export type DialogProps = {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: React.ReactNode;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  size?: "sm" | "md" | "lg" | "xl";
  /** prevent closing (e.g. while a request is in flight) */
  busy?: boolean;
};

export function Dialog({ open, onClose, title, description, children, footer, size = "md", busy = false }: DialogProps) {
  const ref = React.useRef<HTMLDialogElement>(null);
  const opener = React.useRef<Element | null>(null);
  const id = React.useId();

  React.useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      opener.current = document.activeElement;
      d.showModal();
    } else if (!open && d.open) {
      d.close();
      if (opener.current instanceof HTMLElement) opener.current.focus();
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={`${id}-title`}
      aria-describedby={description ? `${id}-desc` : undefined}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onClose();
      }}
      onClick={(e) => {
        if (e.target === ref.current && !busy) onClose();
      }}
      className={cn(
        "w-[calc(100%-2rem)] rounded-lg border border-border bg-surface p-0 text-fg shadow-xl backdrop:bg-black/50",
        size === "sm" && "max-w-sm",
        size === "md" && "max-w-lg",
        size === "lg" && "max-w-2xl",
        size === "xl" && "max-w-4xl",
      )}
    >
      {open ? (
        <div className="flex max-h-[85vh] flex-col">
          <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
            <div>
              <h2 id={`${id}-title`} className="text-base font-semibold">
                {title}
              </h2>
              {description ? (
                <div id={`${id}-desc`} className="mt-1 text-sm text-muted">
                  {description}
                </div>
              ) : null}
            </div>
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              aria-label="Close dialog"
              className="rounded-md p-1 text-muted hover:bg-surface-2 hover:text-fg disabled:opacity-50"
            >
              <X className="h-4 w-4" aria-hidden />
            </button>
          </div>
          <div className="overflow-y-auto px-5 py-4">{children}</div>
          {footer ? <div className="flex justify-end gap-2 border-t border-border px-5 py-3">{footer}</div> : null}
        </div>
      ) : null}
    </dialog>
  );
}
