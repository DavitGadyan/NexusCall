"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { NavSection } from "@/lib/nav";
import { cn } from "@/lib/cn";

export function NavLinks({ sections }: { sections: NavSection[] }) {
  const pathname = usePathname();
  return (
    <div className="flex flex-col gap-4">
      {sections.map((section) => (
        <div key={section.label}>
          <p className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">{section.label}</p>
          <ul className="flex flex-col">
            {section.items.map((item) => {
              const active = pathname === item.href || (item.href !== "/app" && item.href !== "/platform" && pathname.startsWith(item.href + "/"));
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
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
