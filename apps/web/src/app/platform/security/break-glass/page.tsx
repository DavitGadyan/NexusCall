import { PageHeader } from "@/components/ui";
import { BreakGlass } from "@/components/platform/BreakGlass";

export const metadata = { title: "Break-glass access" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function BreakGlassPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const sp = await searchParams;
  const tenant = typeof sp.tenant === "string" && UUID.test(sp.tenant) ? sp.tenant : undefined;
  return (
    <>
      <PageHeader title="Break-glass & support access" description="Time-limited, reason-coded, dual-approved access to a single tenant." />
      <BreakGlass initialTenant={tenant} />
    </>
  );
}
