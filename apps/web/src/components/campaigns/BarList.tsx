/**
 * Horizontal bar list for one magnitude series (contacts per state, per outcome). One hue (brand) for every
 * bar: the bars encode size only, identity is the text label. Rendered as a table so screen readers and
 * keyboard users get the numbers directly; values use text tokens, never the bar colour.
 */
import { humanize } from "@/lib/format";
import { breakdown } from "./lib/campaign-form";

export function BarList({ counts, caption, empty }: { counts: Record<string, number>; caption: string; empty: string }) {
  const rows = breakdown(counts);
  if (rows.length === 0) return <p className="px-5 py-6 text-sm text-muted">{empty}</p>;
  const max = Math.max(...rows.map((r) => r.count), 1);
  return (
    <table className="w-full text-sm">
      <caption className="sr-only">{caption}</caption>
      <thead className="sr-only">
        <tr>
          <th scope="col">Category</th>
          <th scope="col">Count</th>
          <th scope="col">Share</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key} className="group" title={`${humanize(r.key)}: ${r.count.toLocaleString()} (${Math.round(r.share * 100)}%)`}>
            <th scope="row" className="w-36 max-w-[9rem] truncate py-1.5 pl-5 pr-3 text-left font-normal text-fg">
              {humanize(r.key)}
            </th>
            <td className="py-1.5 pr-3">
              <div className="h-2.5 w-full rounded-r bg-transparent" aria-hidden>
                <div className="h-full rounded-r-[4px] bg-brand/80 group-hover:bg-brand" style={{ width: `${Math.max(1, (r.count / max) * 100)}%` }} />
              </div>
            </td>
            <td className="tabular w-24 whitespace-nowrap py-1.5 pr-5 text-right text-fg">
              {r.count.toLocaleString()} <span className="text-xs text-muted">{Math.round(r.share * 100)}%</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
