import { PageHeader } from "@/components/ui";
import { InboundNumbers } from "@/components/calls/InboundNumbers";

export const metadata = { title: "Phone numbers" };

export default function PhoneNumbersPage() {
  return (
    <>
      <PageHeader title="Phone numbers" description="Inbound numbers, the AI agent that answers each one, and when it answers. Changes are audited." />
      <InboundNumbers />
    </>
  );
}
