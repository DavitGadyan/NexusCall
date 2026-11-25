/** Dense, accessible table with loading / error / empty states and cursor pagination ("Load more"). */
import * as React from "react";
import { cn } from "@/lib/cn";
import type { Problem } from "@/lib/problem";
import { ErrorState, LoadingState, Spinner } from "./states";

export type Column<T> = {
  key: string;
  header: React.ReactNode;
  cell: (row: T) => React.ReactNode;
  className?: string;
  /** hide below the md breakpoint to keep small screens readable */
  hideOnMobile?: boolean;
};

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  caption,
  loading,
  error,
  onRetry,
  empty,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  caption: string;
  loading?: boolean;
  error?: { problem: Problem } | null;
  onRetry?: () => void;
  empty?: React.ReactNode;
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
}) {
  if (loading && rows.length === 0) return <LoadingState label={`Loading ${caption.toLowerCase()}`} />;
  if (error && rows.length === 0) return <ErrorState problem={error.problem} onRetry={onRetry} />;
  if (!loading && rows.length === 0) return <>{empty}</>;
  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr>
              {columns.map((c) => (
                <th
                  key={c.key}
                  scope="col"
                  className={cn(
                    "whitespace-nowrap border-b border-border px-4 py-2 text-xs font-medium uppercase tracking-wide text-muted",
                    c.hideOnMobile && "hidden md:table-cell",
                    c.className,
                  )}
                >
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={rowKey(row)} className="hover:bg-surface-2/60">
                {columns.map((c) => (
                  <td key={c.key} className={cn("border-b border-border px-4 py-2 align-middle", c.hideOnMobile && "hidden md:table-cell", c.className)}>
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {error && rows.length > 0 ? <ErrorState problem={error.problem} onRetry={onRetry} /> : null}
      {hasMore && onLoadMore ? (
        <div className="flex justify-center border-t border-border px-4 py-3">
          <button
            type="button"
            onClick={onLoadMore}
            disabled={loadingMore}
            className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-2 disabled:opacity-60"
          >
            {loadingMore ? <Spinner label="Loading more" /> : null}
            Load more
          </button>
        </div>
      ) : null}
    </div>
  );
}
