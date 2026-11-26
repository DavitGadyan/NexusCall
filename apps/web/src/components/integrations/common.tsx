"use client";
import { SubNav } from "@/components/crm/common";

export function IntegrationsNav() {
  return (
    <SubNav
      label="Integration sections"
      items={[
        { href: "/app/integrations", label: "Connections", permission: "integrations:read" },
        { href: "/app/integrations/webhooks", label: "Webhooks", permission: "integrations:read" },
      ]}
    />
  );
}

