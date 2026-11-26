import Link from "next/link";
import { notFound } from "next/navigation";
import { CallDetail } from "@/components/calls/CallDetail";
import { UUID_RE } from "@/components/calls/shared";

export const metadata = { title: "Call" };

export default async function CallDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID_RE.test(id)) notFound();
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/app/calls" className="text-brand hover:underline">
          Call history
        </Link>
      </nav>
      <CallDetail callId={id} />
    </>
  );
}
