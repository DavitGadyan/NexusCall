import Link from "next/link";
import { notFound } from "next/navigation";
import { UUID_RE } from "@/components/calls/shared";
import { DraftReview } from "@/components/inbox/DraftReview";

export const metadata = { title: "Review draft" };

export default async function DraftReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID_RE.test(id)) notFound();
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/app/inbox/drafts" className="text-brand hover:underline">
          Approval queue
        </Link>
      </nav>
      <DraftReview draftId={id} />
    </>
  );
}
