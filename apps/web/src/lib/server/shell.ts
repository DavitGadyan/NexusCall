/**
 * Loads what every authenticated shell needs: the user (`/api/v1/me`), the selected tenant (validated
 * against the user's tenants) and effective permissions in it (`/api/v1/me/access`). Deduplicated per
 * request with React `cache`.
 */
import { headers } from "next/headers";
import { cache } from "react";
import type { Problem } from "@/lib/problem";
import type { Access, Me } from "@/lib/types";
import { type ActiveSession, activeSession, sessions } from "./runtime";
import { upstreamFetch } from "./upstream";

export type ShellContext = {
  active: ActiveSession;
  me: Me;
  tenantId: string | null;
  access: Access | null;
  accessProblem: Problem | null;
};

export type ShellOutcome =
  | { kind: "ok"; ctx: ShellContext }
  | { kind: "signed_out" }
  | { kind: "blocked"; problem: Problem; csrf: string };

/** Picks the session's tenant when it is still one of the user's tenants, else the first one. */
export function chooseTenant(current: string | null, tenants: { id: string }[]): string | null {
  if (current && tenants.some((t) => t.id === current)) return current;
  return tenants[0]?.id ?? null;
}

export const loadShell = cache(async (): Promise<ShellOutcome> => {
  const active = await activeSession();
  if (!active) return { kind: "signed_out" };
  const meResult = await upstreamFetch<Me>("/api/v1/me", { accessToken: active.session.accessToken });
  if (!meResult.ok) {
    if (meResult.status === 401) return { kind: "signed_out" };
    return { kind: "blocked", problem: meResult.problem, csrf: active.session.csrf };
  }
  const me = meResult.data;
  const tenantId = chooseTenant(active.session.tenantId, me.tenants);
  if (tenantId !== active.session.tenantId || Date.now() - active.session.tenantCheckedAt > 60_000) {
    active.session = { ...active.session, tenantId, tenantCheckedAt: Date.now() };
    await sessions().save(active.sid, active.session);
  }
  let access: Access | null = null;
  let accessProblem: Problem | null = null;
  if (tenantId) {
    const accessResult = await upstreamFetch<Access>("/api/v1/me/access", {
      accessToken: active.session.accessToken,
      tenantId,
    });
    if (accessResult.ok) access = accessResult.data;
    else accessProblem = accessResult.problem;
  }
  return { kind: "ok", ctx: { active, me, tenantId, access, accessProblem } };
});

/** Current path + query, as forwarded by the middleware (used for post-login returnTo). */
export async function currentPath(fallback = "/app"): Promise<string> {
  const h = await headers();
  return h.get("x-nexus-path") ?? fallback;
}

export async function loginUrlFor(path?: string): Promise<string> {
  const target = path ?? (await currentPath());
  return `/api/auth/login?returnTo=${encodeURIComponent(target)}`;
}
