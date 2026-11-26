import { PageHeader } from "@/components/ui";
import { AiNav, ModuleGate } from "@/components/crm/common";
import { Knowledge } from "@/components/agents/knowledge/Knowledge";

export const metadata = { title: "Knowledge" };

export default function Page() {
  return (
    <ModuleGate permission="ai:agents:read" what="knowledge">
      <PageHeader title="Knowledge" description="Sources and documents your agents may retrieve from, and a retrieval test." />
      <AiNav />
      <Knowledge />
    </ModuleGate>
  );
}
