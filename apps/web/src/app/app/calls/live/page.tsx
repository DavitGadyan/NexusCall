import Link from "next/link";
import { PageHeader } from "@/components/ui";
import { LiveCalls } from "@/components/calls/LiveCalls";

export const metadata = { title: "Live calls" };

export default function LiveCallsPage() {
  return (
    <>
      <PageHeader
        title="Live calls"
        description="Calls being dialed or in conversation right now. Hanging up is recorded in the audit log."
        actions={
          <Link href="/app/calls" className="text-sm font-medium text-brand hover:underline">
            Call history
          </Link>
        }
      />
      <LiveCalls />
    </>
  );
}
