"use client";
import { CheckCircle2, MailCheck, ShieldAlert } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { api, ApiError, readCsrfToken, toApiError } from "@/lib/client/api";
import { formatDateTime, humanize } from "@/lib/format";
import type { InvitationPreview } from "@/lib/types";
import { Button, ButtonLink, FormError, Notice } from "@/components/ui";
import { StepUpProvider } from "@/components/ui/step-up";
import { ToastProvider } from "@/components/ui/toast";

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <main className="grid min-h-screen place-items-center px-4 py-10">
      <div className="w-full max-w-md rounded-lg border border-border bg-surface p-6 shadow-sm">
        <div className="mb-5 flex items-center gap-2">
          <span className="grid h-8 w-8 place-items-center rounded-md bg-brand text-xs font-bold text-brand-fg" aria-hidden>
            NC
          </span>
          <span className="text-sm font-semibold">NexusCall</span>
        </div>
        {children}
      </div>
    </main>
  );
}

export function InvitationProblem({ kind }: { kind: "invalid" | "rate_limited" | "unavailable" }) {
  const text = {
    invalid: ["This invitation is invalid or has expired", "Invitation links work once and expire. Ask the person who invited you to send a new one."],
    rate_limited: ["Too many attempts", "Wait a minute, then open the link again."],
    unavailable: ["Invitations are unavailable right now", "Try again in a moment."],
  }[kind];
  return (
    <Frame>
      <h1 className="text-lg font-semibold" data-testid="invitation-problem">
        {text[0]}
      </h1>
      <p className="mt-2 text-sm text-muted">{text[1]}</p>
      <Link href="/" className="mt-5 inline-block text-sm font-medium text-brand hover:underline">
        Go to sign-in
      </Link>
    </Frame>
  );
}

export function AcceptInvitation(props: {
  token: string;
  preview: InvitationPreview;
  signedIn: { email: string | null; name: string | null; csrf: string } | null;
}) {
  return (
    <ToastProvider>
      <StepUpProvider>
        <AcceptInvitationInner {...props} />
      </StepUpProvider>
    </ToastProvider>
  );
}

function AcceptInvitationInner({
  token,
  preview,
  signedIn,
}: {
  token: string;
  preview: InvitationPreview;
  signedIn: { email: string | null; name: string | null; csrf: string } | null;
}) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<ApiError | null>(null);
  const [onboarded, setOnboarded] = React.useState(false);
  const [done, setDone] = React.useState(false);
  const here = `/invite/accept?token=${encodeURIComponent(token)}`;

  async function accept() {
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ tenant_id: string }>("/api/v1/invitations/accept", { method: "POST", body: { token } });
      setDone(true);
      const resp = await fetch("/api/session/tenant", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": readCsrfToken() ?? "" },
        body: JSON.stringify({ tenant_id: result.tenant_id }),
        credentials: "same-origin",
      });
      window.location.assign(resp.ok ? "/app" : "/");
    } catch (err) {
      setError(toApiError(err));
      setBusy(false);
    }
  }

  async function onboard() {
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/invitations/onboard", { method: "POST", body: { token }, quietAuth: true });
      setOnboarded(true);
    } catch (err) {
      setError(toApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Frame>
      <h1 className="text-lg font-semibold">You&apos;re invited to join {preview.tenant_name}</h1>
      <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
        <dt className="text-muted">Invited email</dt>
        <dd className="font-mono">{preview.email_masked}</dd>
        <dt className="text-muted">Role</dt>
        <dd>{preview.role_keys.map(humanize).join(", ")}</dd>
        <dt className="text-muted">Expires</dt>
        <dd>{formatDateTime(preview.expires_at)}</dd>
      </dl>

      <div className="mt-6 flex flex-col gap-3">
        {done ? (
          <Notice tone="ok" title="Invitation accepted">
            <span className="inline-flex items-center gap-1">
              <CheckCircle2 className="h-4 w-4" aria-hidden /> Opening your workspace…
            </span>
          </Notice>
        ) : signedIn ? (
          <>
            <p className="text-sm text-muted">
              Signed in as <span className="font-medium text-fg">{signedIn.email ?? signedIn.name}</span>. Accept with the account the invitation was sent to.
            </p>
            {error?.isForbidden ? (
              <Notice tone="warn" title="This invitation is for a different account">
                <p>{error.message}</p>
                <form action="/api/auth/logout" method="post" className="mt-2">
                  <input type="hidden" name="csrf_token" value={signedIn.csrf} />
                  <input type="hidden" name="returnTo" value={here} />
                  <button type="submit" className="text-sm font-medium text-brand hover:underline">
                    Sign out and use another account
                  </button>
                </form>
              </Notice>
            ) : error?.isStepUp ? (
              <Notice tone="info" title="Multi-factor sign-in required">
                <span className="inline-flex items-center gap-1">
                  <ShieldAlert className="h-4 w-4" aria-hidden /> This role requires MFA. Verify, then accept again.
                </span>
              </Notice>
            ) : (
              <FormError message={error?.message} />
            )}
            <Button onClick={accept} disabled={busy} data-testid="accept-invitation">
              {busy ? "Accepting…" : "Accept invitation"}
            </Button>
          </>
        ) : onboarded ? (
          <Notice tone="ok" title="Check your email">
            <span className="inline-flex items-start gap-2">
              <MailCheck className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              If this invitation is valid, we sent {preview.email_masked} a link to verify the address and set a password
              (and an authenticator for administrator roles). It brings you back here to accept.
            </span>
          </Notice>
        ) : (
          <>
            <ButtonLink variant="primary" href={`/api/auth/login?returnTo=${encodeURIComponent(here)}`} data-testid="invite-sign-in">
              Sign in to accept
            </ButtonLink>
            <Button variant="secondary" onClick={onboard} disabled={busy}>
              {busy ? "Sending…" : "Set up my account"}
            </Button>
            <p className="text-xs text-muted">
              New to NexusCall? &ldquo;Set up my account&rdquo; emails you a secure link to create your sign-in. We never send passwords by email.
            </p>
            <FormError message={error?.message} />
          </>
        )}
      </div>
    </Frame>
  );
}
