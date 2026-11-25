import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "NexusCall", template: "%s · NexusCall" },
  description: "Multi-tenant AI call center and agency platform",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Reading the per-request nonce makes every page dynamic, so Next.js stamps its scripts with the nonce
  // from the middleware's CSP (no static HTML without nonces).
  await headers();
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
