"use client";
/** Toasts announced through aria-live regions (polite for info/success, assertive for errors). */
import { X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

export type ToastTone = "info" | "success" | "error";
type ToastItem = { id: number; title: string; description?: string; tone: ToastTone };
type ToastFn = (t: { title: string; description?: string; tone?: ToastTone }) => void;

const ToastCtx = React.createContext<ToastFn | null>(null);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = React.useState<ToastItem[]>([]);
  const next = React.useRef(1);

  const dismiss = React.useCallback((id: number) => setItems((all) => all.filter((t) => t.id !== id)), []);
  const toast = React.useCallback<ToastFn>(
    ({ title, description, tone = "info" }) => {
      const id = next.current++;
      setItems((all) => [...all.slice(-4), { id, title, description, tone }]);
      setTimeout(() => dismiss(id), tone === "error" ? 9000 : 5000);
    },
    [dismiss],
  );

  const render = (tone: "polite" | "assertive") =>
    items
      .filter((t) => (tone === "assertive" ? t.tone === "error" : t.tone !== "error"))
      .map((t) => (
        <div
          key={t.id}
          className={cn(
            "pointer-events-auto flex w-80 max-w-[calc(100vw-2rem)] items-start gap-3 rounded-lg border bg-surface px-4 py-3 text-sm shadow-lg",
            t.tone === "error" ? "border-danger/40" : t.tone === "success" ? "border-ok/40" : "border-border",
          )}
        >
          <div className="min-w-0 flex-1">
            <p className={cn("font-medium", t.tone === "error" && "text-danger", t.tone === "success" && "text-ok")}>{t.title}</p>
            {t.description ? <p className="mt-0.5 break-words text-muted">{t.description}</p> : null}
          </div>
          <button type="button" onClick={() => dismiss(t.id)} aria-label="Dismiss notification" className="text-muted hover:text-fg">
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
      ));

  return (
    <ToastCtx.Provider value={toast}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col gap-2">
        <div role="status" aria-live="polite" className="flex flex-col gap-2">
          {render("polite")}
        </div>
        <div role="alert" aria-live="assertive" className="flex flex-col gap-2">
          {render("assertive")}
        </div>
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast(): ToastFn {
  const ctx = React.useContext(ToastCtx);
  return ctx ?? (() => undefined);
}
