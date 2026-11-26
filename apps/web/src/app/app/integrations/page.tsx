import { PageHeader } from "@/components/ui";
import { IntegrationsNav } from "@/components/integrations/common";
import { Connections } from "@/components/integrations/Connections";
import { oauthQueryFrom } from "@/components/integrations/lib/connections";
import { PermissionGate, WORKSPACE_BACK } from "@/components/platform-ops/common";

export const metadata = { title: "Integrations" };

export default async function Page({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  return (
    <PermissionGate permission="integrations:read" what="integrations" back={WORKSPACE_BACK}>
      <PageHeader
        title="Integrations"
        description="Your own provider accounts (telephony, email, calendar, CRM) and outbound webhooks. Stored credentials are never shown."
      />
      <IntegrationsNav />
      <Connections basePath="/app/integrations/connections" newHref="/app/integrations/connections/new" oauthQuery={oauthQueryFrom(sp)} />
    </PermissionGate>
  );
}
