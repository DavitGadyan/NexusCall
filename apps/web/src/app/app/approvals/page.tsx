import { PageHeader } from "@/components/ui";
import { ApprovalQueue } from "./_components/ToolApprovals";

export const metadata = { title: "AI approvals" };

export default async function ApprovalsPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  return (
    <>
      <PageHeader
        title="AI approvals"
        description="Customer-facing actions AI agents proposed and that need a person. Approving requires a recent MFA sign-in; every decision is audited."
      />
      <ApprovalQueue initialTab={typeof sp.tab === "string" ? sp.tab : undefined} />
    </>
  );
}
