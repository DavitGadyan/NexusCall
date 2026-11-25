"use client";
/**
 * Step-up authentication (build.md §20.2).
 * - `StepUpProvider` listens for `step_up_required` answers from the API wrapper and offers re-authentication.
 * - `StepUpGate` guards a high-risk area up front: without a recent MFA sign-in it shows a verify prompt
 *   instead of its children. The backend still enforces step-up on every call.
 */
import { ShieldCheck } from "lucide-react";
import * as React from "react";
import { hasRecentStepUp } from "@/lib/access";
import { STEP_UP_EVENT, stepUpLoginUrl } from "@/lib/client/api";
import { useOptionalSession } from "@/components/shell/session-context";
import { Dialog } from "./dialog";
import { Button } from "./primitives";

export function StepUpProvider({ children }: { children: React.ReactNode }) {
  const [message, setMessage] = React.useState<string | null>(null);
  React.useEffect(() => {
    const onStepUp = (e: Event) => {
      const detail = (e as CustomEvent<{ message?: string }>).detail;
      setMessage(detail?.message ?? "This action needs a recent multi-factor sign-in.");
    };
    window.addEventListener(STEP_UP_EVENT, onStepUp);
    return () => window.removeEventListener(STEP_UP_EVENT, onStepUp);
  }, []);
  return (
    <>
      {children}
      <Dialog
        open={message !== null}
        onClose={() => setMessage(null)}
        title="Confirm it's you"
        description={message}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setMessage(null)}>
              Not now
            </Button>
            <Button onClick={() => window.location.assign(stepUpLoginUrl())}>
              <ShieldCheck className="h-4 w-4" aria-hidden /> Verify with MFA
            </Button>
          </>
        }
      >
        <p className="text-sm text-muted">
          You will sign in again with your authenticator and come back to this page. Then repeat the action.
        </p>
      </Dialog>
    </>
  );
}

export function useRecentStepUp(maxAgeSeconds = 300): boolean {
  const session = useOptionalSession();
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(t);
  }, []);
  return hasRecentStepUp(session?.acr ?? null, session?.authTime ?? null, now, maxAgeSeconds);
}

export function StepUpGate({
  children,
  maxAgeSeconds = 300,
  title = "Verification required",
  description = "This area changes security-critical settings. Verify with your authenticator to continue; the verification lasts five minutes.",
}: {
  children: React.ReactNode;
  maxAgeSeconds?: number;
  title?: string;
  description?: string;
}) {
  const ok = useRecentStepUp(maxAgeSeconds);
  if (ok) return <>{children}</>;
  return (
    <div className="flex flex-col items-start gap-3 rounded-lg border border-warn/40 bg-warn/5 p-5">
      <div className="flex items-center gap-2">
        <ShieldCheck className="h-5 w-5 text-warn" aria-hidden />
        <p className="text-sm font-semibold">{title}</p>
      </div>
      <p className="max-w-xl text-sm text-muted">{description}</p>
      <a href={stepUpLoginUrl()} className="inline-flex h-9 items-center gap-2 rounded-md bg-brand px-4 text-sm font-medium text-brand-fg hover:bg-brand/90">
        Verify with MFA
      </a>
    </div>
  );
}
