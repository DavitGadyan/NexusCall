"use client";
import { SubNav } from "@/components/crm/common";

export function AnalyticsNav() {
  return (
    <SubNav
      label="Analytics sections"
      items={[
        { href: "/app/analytics", label: "Overview", permission: "analytics:read" },
        { href: "/app/analytics/campaigns", label: "Campaigns", permission: "analytics:read" },
        { href: "/app/analytics/exports", label: "Exports", permission: "analytics:export" },
      ]}
    />
  );
}
