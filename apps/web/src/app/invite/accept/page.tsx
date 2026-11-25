/**
 * Invitation acceptance (build.md §18.3): preview → sign in (or set up the account) → accept → land in the
 * tenant. The preview is fetched server-side; responses are uniform for invalid, used and expired tokens.
 */
import { headers } from "next/headers";
import { AcceptInvitation, InvitationProblem } from "@/components/invite/AcceptInvitation";
import { clientIpFrom } from "@/lib/server/proxy";
import { activeSession } from "@/lib/server/runtime";
import { upstreamFetch } from "@/lib/server/upstream";
import type { InvitationPreview } from "@/lib/types";

export const metadata = { title: "Accept invitation", referrer: "no-referrer" as const };

const TOKEN = /^[A-Za-z0-9_-]{20,128}$/;

export default async function AcceptInvitationPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const params = await searchParams;
  const token = typeof params.token === "string" ? params.token : "";
  if (!TOKEN.test(token)) return <InvitationProblem kind="invalid" />;

  const preview = await upstreamFetch<InvitationPreview>("/api/v1/invitations/preview", {
    method: "POST",
    body: { token },
    accessToken: null,
    clientIp: clientIpFrom(await headers()),
  });
  if (!preview.ok) {
    return <InvitationProblem kind={preview.status === 404 || preview.status === 422 ? "invalid" : preview.status === 429 ? "rate_limited" : "unavailable"} />;
  }

  let signedIn: { email: string | null; name: string | null; csrf: string } | null = null;
  try {
    const active = await activeSession();
    if (active) signedIn = { email: active.session.email, name: active.session.name, csrf: active.session.csrf };
  } catch {
    signedIn = null;
  }

  return <AcceptInvitation token={token} preview={preview.data} signedIn={signedIn} />;
}
