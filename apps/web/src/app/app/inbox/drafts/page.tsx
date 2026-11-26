import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { DraftQueue } from "@/components/inbox/Inbox";

export const metadata = { title: "Approval queue" };

export default function DraftQueuePage() {
  return (
    <>
      <PageHeader
        title="Approval queue"
        description="AI-written reply drafts. None is sent until a person with approval rights approves the exact text."
        actions={
          <Link href="/app/inbox" className="text-sm font-medium text-brand hover:underline">
            Inbox
          </Link>
        }
      />
      <DraftQueue />
    </>
  );
}
