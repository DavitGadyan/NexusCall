"use client";
/** Date range filter: presets first, then a custom UTC range. One row, above everything it scopes. */
import * as React from "react";
import { cn } from "@/lib/cn";
import { Button, Input } from "@/components/ui";
import { type DateRange, matchPreset, PRESETS, presetRange, utcToday, validateRange } from "./lib/dates";

export function RangeFilter({ value, onChange }: { value: DateRange; onChange: (r: DateRange) => void }) {
  const active = matchPreset(value);
  const [draft, setDraft] = React.useState<DateRange>(value);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => setDraft(value), [value]);

  function apply(e: React.FormEvent) {
    e.preventDefault();
    const err = validateRange(draft);
    setError(err);
    if (!err) onChange(draft);
  }

  return (
    <form onSubmit={apply} className="mb-4 flex flex-wrap items-end gap-3" aria-label="Date range">
      <div role="group" aria-label="Presets" className="flex rounded-md border border-border bg-surface p-0.5">
        {PRESETS.map((p) => (
          <button
            key={p.days}
            type="button"
            aria-pressed={active === p.days}
            onClick={() => {
              setError(null);
              onChange(presetRange(p.days));
            }}
            className={cn("rounded px-3 py-1.5 text-sm", active === p.days ? "bg-surface-2 font-semibold" : "text-muted hover:text-fg")}
          >
            {p.label}
          </button>
        ))}
      </div>
      <label className="flex flex-col gap-1 text-xs text-muted">
        From (UTC)
        <Input type="date" value={draft.from} max={utcToday()} onChange={(e) => setDraft((d) => ({ ...d, from: e.target.value }))} className="w-40" />
      </label>
      <label className="flex flex-col gap-1 text-xs text-muted">
        To (UTC)
        <Input type="date" value={draft.to} max={utcToday()} onChange={(e) => setDraft((d) => ({ ...d, to: e.target.value }))} className="w-40" />
      </label>
      <Button type="submit" variant="secondary">
        Apply
      </Button>
      {error ? (
        <p role="alert" className="w-full text-xs text-danger">
          {error}
        </p>
      ) : null}
    </form>
  );
}
