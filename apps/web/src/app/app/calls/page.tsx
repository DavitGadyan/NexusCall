import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { CallHistory } from "@/components/calls/CallHistory";

export const metadata = { title: "Call history" };

type SP = Promise<Record<string, string | string[] | undefined>>;
const one = (v: string | string[] | undefined) => (typeof v === "string" ? v : undefined);

export default async function CallHistoryPage({ searchParams }: { searchParams: SP }) {
  const sp = await searchParams;
  return (
    <>
      <PageHeader
        title="Call history"
        description="Every inbound, outbound and test call in this workspace. Phone numbers are masked."
        actions={
          <Link href="/app/calls/live" className="text-sm font-medium text-brand hover:underline">
            Live calls
          </Link>
        }
      />
      <CallHistory initial={{ state: one(sp.state), direction: one(sp.direction), campaign_id: one(sp.campaign_id), contact_id: one(sp.contact_id) }} />
    </>
  );
}
