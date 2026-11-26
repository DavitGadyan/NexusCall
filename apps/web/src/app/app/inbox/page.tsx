import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { ThreadList } from "@/components/inbox/Inbox";

export const metadata = { title: "Inbox" };

export default function InboxPage() {
  return (
    <>
      <PageHeader
        title="Inbox"
        description="Replies to your mailboxes. Email content is shown in an isolated frame with remote images blocked; attachments open only after a clean malware scan."
        actions={
          <Link href="/app/inbox/drafts" className="text-sm font-medium text-brand hover:underline">
            Approval queue
          </Link>
        }
      />
      <ThreadList />
    </>
  );
}
