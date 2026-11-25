import { redirect } from "next/navigation";
import { hasPermission } from "@/lib/permissions";
import { loadShell } from "@/lib/server/shell";

export default async function TenantAdminHome() {
  const outcome = await loadShell();
  const perms = outcome.kind === "ok" ? (outcome.ctx.access?.permissions ?? []) : [];
  if (hasPermission(perms, "tenant:users:manage")) redirect("/admin/team");
  if (hasPermission(perms, "tenant:manage")) redirect("/admin/settings");
  if (hasPermission(perms, "audit:read")) redirect("/admin/audit");
  return null;
}
