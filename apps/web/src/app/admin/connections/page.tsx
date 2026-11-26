import { PageHeader } from "@/components/ui";
import { Connections } from "@/components/integrations/Connections";
import { oauthQueryFrom } from "@/components/integrations/lib/connections";
import { ADMIN_BACK, PermissionGate } from "@/components/platform-ops/common";

export const metadata = { title: "Provider connections" };

export default async function Page({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  return (
    <PermissionGate permission="integrations:read" what="provider connections" back={ADMIN_BACK}>
      <PageHeader
        title="Provider connections"
        description="Connect this workspace's own provider accounts. Credentials are write-only, tested server-side, rotated on schedule and audited."
      />
      <Connections basePath="/admin/connections" newHref="/admin/connections/new" oauthQuery={oauthQueryFrom(sp)} />
    </PermissionGate>
  );
}
