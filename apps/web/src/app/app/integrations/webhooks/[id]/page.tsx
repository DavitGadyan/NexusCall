import { notFound } from "next/navigation";
import { IntegrationsNav } from "@/components/integrations/common";
import { WebhookDetail } from "@/components/integrations/WebhookDetail";
import { PermissionGate, WORKSPACE_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "Webhook endpoint" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound();
  return (
    <PermissionGate permission="integrations:read" what="webhooks" back={WORKSPACE_BACK}>
      <IntegrationsNav />
      <WebhookDetail endpointId={id} />
    </PermissionGate>
  );
}
