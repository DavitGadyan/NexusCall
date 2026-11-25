"use client";
import { StepUpProvider } from "@/components/ui/step-up";
import { ToastProvider } from "@/components/ui/toast";
import { type ClientSession, SessionProvider } from "./session-context";

export function ShellProviders({ session, children }: { session: ClientSession; children: React.ReactNode }) {
  return (
    <SessionProvider value={session}>
      <ToastProvider>
        <StepUpProvider>{children}</StepUpProvider>
      </ToastProvider>
    </SessionProvider>
  );
}
