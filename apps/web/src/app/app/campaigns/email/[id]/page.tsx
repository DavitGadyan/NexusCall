import Link from "next/link";
import { notFound } from "next/navigation";
import { UUID_RE } from "@/components/calls/shared";
import { CampaignDetail } from "@/components/email/Campaigns";

export const metadata = { title: "Email campaign" };

export default async function EmailCampaignPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID_RE.test(id)) notFound();
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-2 text-sm">
        <Link href="/app/campaigns/email" className="text-brand hover:underline">
          Email campaigns
        </Link>
      </nav>
      <CampaignDetail campaignId={id} />
    </>
  );
}
