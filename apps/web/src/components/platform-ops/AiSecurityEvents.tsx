"use client";
/** Cross-tenant AI security events (read-only; the gateway audits every view of this list). */
import * as React from "react";
import { withQuery } from "@/lib/client/api";
import { useCursorList } from "@/lib/client/use-api";
import { Card, CardHeader, Notice, Select } from "@/components/ui";
import { SEVERITIES, type SecurityEvent } from "./lib/ai";
import { SecurityEventsTable } from "./TenantAi";

export function AiSecurityEvents() {
  const [min, setMin] = React.useState<(typeof SEVERITIES)[number]>("low");
  const list = useCursorList<SecurityEvent>(withQuery("/api/v1/platform/ai/security-events", { min_severity: min }));
  return (
    <div className="flex flex-col gap-4">
      <Notice tone="info">Viewing this cross-tenant list is recorded in the audit log. Events carry identifiers and short summaries only, never prompt or completion text.</Notice>
      <Card>
        <CardHeader
          title="Security events"
          description="Prompt-injection attempts, blocked outputs, denied tools and budget or policy violations across tenants."
          actions={
            <label className="flex items-center gap-2 text-sm">
              <span className="text-muted">Minimum severity</span>
              <Select value={min} onChange={(e) => setMin(e.target.value as (typeof SEVERITIES)[number])} className="w-32">
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </Select>
            </label>
          }
        />
        <SecurityEventsTable list={list} showTenant />
      </Card>
    </div>
  );
}
