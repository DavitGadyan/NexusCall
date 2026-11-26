import Link from "next/link";
import { notFound } from "next/navigation";
import { UUID_RE } from "@/components/calls/shared";
import { ThreadView } from "@/components/inbox/Inbox";

export const metadata = { title: "Email thread" };

export default async function ThreadPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID_RE.test(id)) notFound();
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/app/inbox" className="text-brand hover:underline">
          Inbox
        </Link>
      </nav>
      <ThreadView threadId={id} />
    </>
  );
}
