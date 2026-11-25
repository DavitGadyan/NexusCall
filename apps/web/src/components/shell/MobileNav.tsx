"use client";
/** Navigation drawer for small screens (the sidebar is hidden below md). */
import { Menu, X } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import type { NavSection } from "@/lib/nav";
import { NavLinks } from "./NavLinks";

export function MobileNav({ label, sections, others }: { label: string; sections: NavSection[]; others: { label: string; href: string }[] }) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDialogElement>(null);

  React.useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  return (
    <div className="md:hidden">
      <button type="button" onClick={() => setOpen(true)} aria-label="Open navigation" className="rounded-md p-1.5 hover:bg-surface-2">
        <Menu className="h-5 w-5" aria-hidden />
      </button>
      <dialog
        ref={ref}
        aria-label={label}
        onCancel={(e) => {
          e.preventDefault();
          setOpen(false);
        }}
        onClick={(e) => {
          if (e.target === ref.current) setOpen(false);
        }}
        className="m-0 h-full max-h-full w-72 max-w-[85vw] border-r border-border bg-surface p-0 text-fg backdrop:bg-black/40"
      >
        <div className="flex h-14 items-center justify-between border-b border-border px-4">
          <span className="text-sm font-semibold">NexusCall</span>
          <button type="button" onClick={() => setOpen(false)} aria-label="Close navigation" className="rounded-md p-1 hover:bg-surface-2">
            <X className="h-5 w-5" aria-hidden />
          </button>
        </div>
        <nav aria-label={label} className="px-2 py-3">
          <NavLinks sections={sections} onNavigate={() => setOpen(false)} />
          {others.length > 0 ? (
            <div className="mt-4 flex flex-col gap-1 border-t border-border px-2 pt-3 text-sm text-muted">
              {others.map((o) => (
                <Link key={o.href} href={o.href} onClick={() => setOpen(false)} className="hover:text-fg">
                  → {o.label}
                </Link>
              ))}
            </div>
          ) : null}
        </nav>
      </dialog>
    </div>
  );
}
