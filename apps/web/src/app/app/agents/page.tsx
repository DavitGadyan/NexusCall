import { PageHeader } from "@/components/ui";
import { AiNav, ModuleGate } from "@/components/crm/common";
import { Agents } from "@/components/agents/Agents";

export const metadata = { title: "AI agents" };

export default function Page() {
  return (
    <ModuleGate permission="ai:agents:read" what="AI agents">
      <PageHeader title="AI agents" description="Configure voice and email agents, their prompt versions and tools." />
      <AiNav />
      <Agents />
    </ModuleGate>
  );
}
