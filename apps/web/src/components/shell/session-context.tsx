"use client";
/**
 * What client components may know about the signed-in user: identity, MFA level and effective permissions
 * for UI gating. Never tokens. The backend remains the authority for every decision.
 */
import { createContext, useContext, useMemo } from "react";
import { hasPermission } from "@/lib/permissions";
import type { TenantRef } from "@/lib/types";

export type ClientSession = {
  user: { id: string; email: string | null; name: string | null };
  acr: string | null;
  authTime: number | null;
  tenants: TenantRef[];
  tenantId: string | null;
  roles: string[];
  permissions: string[];
  platformPermissions: string[];
};

const Ctx = createContext<ClientSession | null>(null);

export function SessionProvider({ value, children }: { value: ClientSession; children: React.ReactNode }) {
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): ClientSession {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSession must be used inside SessionProvider");
  return v;
}

export function useOptionalSession(): ClientSession | null {
  return useContext(Ctx);
}

/** UI gating only. Platform permissions are checked against the platform set. */
export function useCan(): (permission: string) => boolean {
  const s = useOptionalSession();
  return useMemo(() => {
    const tenant = new Set(s?.permissions ?? []);
    const platform = new Set(s?.platformPermissions ?? []);
    return (permission: string) => hasPermission(permission.startsWith("platform:") ? platform : tenant, permission);
  }, [s]);
}

export function Can({ permission, children, fallback = null }: { permission: string; children: React.ReactNode; fallback?: React.ReactNode }) {
  const can = useCan();
  return <>{can(permission) ? children : fallback}</>;
}
