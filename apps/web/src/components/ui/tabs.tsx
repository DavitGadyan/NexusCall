"use client";
/** WAI-ARIA tabs with roving tabindex (Arrow keys, Home, End). */
import * as React from "react";
import { cn } from "@/lib/cn";

export type TabItem = { id: string; label: React.ReactNode; badge?: React.ReactNode };

export function Tabs({
  tabs,
  value,
  onChange,
  label,
  children,
  className,
}: {
  tabs: TabItem[];
  value: string;
  onChange: (id: string) => void;
  label: string;
  /** the active panel's content */
  children?: React.ReactNode;
  className?: string;
}) {
  const base = React.useId();
  const refs = React.useRef<(HTMLButtonElement | null)[]>([]);
  const index = Math.max(0, tabs.findIndex((t) => t.id === value));

  function onKeyDown(e: React.KeyboardEvent) {
    let next = index;
    if (e.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (e.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    else return;
    e.preventDefault();
    const t = tabs[next];
    if (t) {
      onChange(t.id);
      refs.current[next]?.focus();
    }
  }

  return (
    <div className={className}>
      <div role="tablist" aria-label={label} onKeyDown={onKeyDown} className="flex gap-1 overflow-x-auto border-b border-border">
        {tabs.map((t, i) => {
          const selected = i === index;
          return (
            <button
              key={t.id}
              ref={(el) => {
                refs.current[i] = el;
              }}
              role="tab"
              type="button"
              id={`${base}-tab-${t.id}`}
              aria-selected={selected}
              aria-controls={`${base}-panel`}
              tabIndex={selected ? 0 : -1}
              onClick={() => onChange(t.id)}
              className={cn(
                "-mb-px inline-flex shrink-0 items-center gap-2 whitespace-nowrap border-b-2 px-3 py-2 text-sm",
                selected ? "border-brand font-medium text-fg" : "border-transparent text-muted hover:text-fg",
              )}
            >
              {t.label}
              {t.badge}
            </button>
          );
        })}
      </div>
      <div role="tabpanel" id={`${base}-panel`} aria-labelledby={`${base}-tab-${tabs[index]?.id ?? ""}`} tabIndex={0} className="pt-4 focus:outline-none">
        {children}
      </div>
    </div>
  );
}
