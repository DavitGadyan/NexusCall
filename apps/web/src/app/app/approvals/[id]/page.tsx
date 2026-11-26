import Link from "next/link";
import { notFound } from "next/navigation";
import { UUID_RE } from "@/components/calls/shared";
import { ApprovalDetail } from "../_components/ToolApprovals";

export const metadata = { title: "AI approval" };

export default async function ApprovalDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID_RE.test(id)) notFound();
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/app/approvals" className="text-brand hover:underline">
          AI approvals
        </Link>
      </nav>
      <ApprovalDetail approvalId={id} />
    </>
  );
}
