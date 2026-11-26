import { notFound } from "next/navigation";
import { ConnectionDetail } from "@/components/integrations/ConnectionDetail";
import { ADMIN_BACK, PermissionGate } from "@/components/platform-ops/common";

export const metadata = { title: "Connection" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID.test(id)) notFound();
  return (
    <PermissionGate permission="integrations:read" what="provider connections" back={ADMIN_BACK}>
      <ConnectionDetail connectionId={id} listHref="/admin/connections" />
    </PermissionGate>
  );
}
