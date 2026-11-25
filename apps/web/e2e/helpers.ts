/**
 * Shared helpers for the e2e specs. Dev users and the TOTP secret come from scripts/gen_keycloak_realm.py:
 * the Keycloak OTP credential stores the raw secret string, and authenticator apps use base32(raw).
 */
import { expect, type Page } from "@playwright/test";
import { authenticator } from "otplib";

export const DEV_TOTP_RAW = "nexusdevtotpsecret01";

export const USERS = {
  platformOwner: "platform.owner@nexus.local",
  platformOperator: "platform.operator@nexus.local",
  acmeOwner: "owner@acme.test",
  acmeAdmin: "admin@acme.test",
  analyst: "analyst@acme.test",
  globexOwner: "owner@globex.test",
  multi: "multi@both.test",
} as const;

export const TENANTS = { acme: "Acme Home Services", globex: "Globex Dental" } as const;

export function password(): string {
  const value = process.env.KC_DEV_USER_PASSWORD;
  if (!value) throw new Error("Set KC_DEV_USER_PASSWORD (from your local .env) to run the e2e specs");
  return value;
}

export function base32(raw: string): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  let out = "";
  for (const byte of Buffer.from(raw, "utf8")) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      out += alphabet[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += alphabet[(value << (5 - bits)) & 31];
  return out;
}

export function totp(): string {
  return authenticator.generate(base32(DEV_TOTP_RAW));
}

/** Completes the Keycloak login form(s); enters a TOTP code when the OTP form appears. */
export async function completeKeycloakLogin(page: Page, email: string): Promise<void> {
  const username = page.locator("#username");
  if (await username.isVisible({ timeout: 10_000 }).catch(() => false)) {
    await username.fill(email);
    await page.locator("#password").fill(password());
    await page.locator("#kc-login").click();
  }
  const otp = page.locator("#otp");
  if (await otp.isVisible({ timeout: 5_000 }).catch(() => false)) {
    await otp.fill(totp());
    await page.locator("#kc-login").click();
  }
}

/** Signs in through the BFF. `stepUp` requests aal2 (password + OTP). */
export async function signIn(page: Page, email: string, opts: { stepUp?: boolean; returnTo?: string } = {}): Promise<void> {
  const params = new URLSearchParams();
  if (opts.stepUp) params.set("stepup", "1");
  if (opts.returnTo) params.set("returnTo", opts.returnTo);
  await page.goto(`/api/auth/login${params.size ? `?${params}` : ""}`);
  await completeKeycloakLogin(page, email);
  await page.waitForURL((url) => !url.pathname.includes("/realms/") && !url.pathname.startsWith("/api/auth/"), { timeout: 20_000 });
}

export async function signOut(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Sign out" }).first().click();
  await expect(page).toHaveURL(/signed_out=1|\/invite\//);
}

export function uniqueSuffix(): string {
  return `${Date.now().toString(36)}${Math.floor(Math.random() * 1e4).toString(36)}`;
}
