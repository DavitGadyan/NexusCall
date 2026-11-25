"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { NavSection } from "@/lib/nav";
import { cn } from "@/lib/cn";

const ROOTS = new Set(["/app", "/admin", "/platform"]);

export function isActiveHref(pathname: string, href: string, allHrefs: readonly string[]): boolean {
  if (pathname === href) return true;
  if (ROOTS.has(href) || !pathname.startsWith(`${href}/`)) return false;
  // Prefer the most specific item: "/app/calls/live" should not also light up "/app/calls".
  return !allHrefs.some((other) => other !== href && other.startsWith(`${href}/`) && (pathname === other || pathname.startsWith(`${other}/`)));
}

export function NavLinks({ sections, onNavigate }: { sections: NavSection[]; onNavigate?: () => void }) {
  const pathname = usePathname();
  const all = sections.flatMap((s) => s.items.map((i) => i.href));
  return (
    <div className="flex flex-col gap-4">
      {sections.map((section) => (
        <div key={section.label}>
          <p className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">{section.label}</p>
          <ul className="flex flex-col">
            {section.items.map((item) => {
              const active = isActiveHref(pathname, item.href, all);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "block rounded-md px-2 py-1.5 text-sm",
                      active ? "bg-brand/10 font-medium text-brand" : "text-fg/80 hover:bg-surface-2 hover:text-fg",
                    )}
                  >
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}
