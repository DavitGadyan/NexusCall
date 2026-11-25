import { notFound } from "next/navigation";
import { UserDetail } from "@/components/platform/UserDetail";

export const metadata = { title: "User" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function UserDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { id } = await params;
  const sp = await searchParams;
  if (!UUID.test(id)) notFound();
  const tenant = typeof sp.tenant === "string" && UUID.test(sp.tenant) ? sp.tenant : undefined;
  return <UserDetail userId={id} initialTenant={tenant} />;
}
