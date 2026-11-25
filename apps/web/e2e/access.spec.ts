import { expect, test } from "@playwright/test";
import { signIn, USERS } from "./helpers";

test("a tenant admin is blocked from the platform console (UI and API)", async ({ page }) => {
  await signIn(page, USERS.acmeAdmin, { returnTo: "/app" });
  await expect(page.getByRole("link", { name: "→ Platform admin" })).toHaveCount(0);

  await page.goto("/platform");
  await expect(page.getByTestId("access-denied")).toBeVisible();
  await expect(page.getByText("You don't have access to the platform console")).toBeVisible();
  await expect(page.getByRole("navigation").getByRole("link", { name: "Tenants" })).toHaveCount(0);

  for (const path of ["/api/v1/platform/tenants", "/api/v1/platform/users", "/api/v1/platform/rls/scans/latest", "/api/v1/platform/settings"]) {
    const status = await page.evaluate(async (p) => (await fetch(`/api/bff${p}`)).status, path);
    expect(status, path).toBe(403);
  }
});

test("an analyst cannot open tenant administration", async ({ page }) => {
  await signIn(page, USERS.analyst, { returnTo: "/admin/team" });
  await expect(page.getByTestId("access-denied")).toBeVisible();
  const status = await page.evaluate(async () => (await fetch("/api/bff/api/v1/memberships")).status);
  expect(status).toBe(403);
});

test("the proxy refuses paths outside /api/v1", async ({ page }) => {
  await signIn(page, USERS.acmeAdmin, { returnTo: "/app" });
  for (const path of ["/api/bff/internal/v1/authz/resolve", "/api/bff/api/v1/..%2F..%2Finternal%2Fv1%2Ftenants", "/api/bff/webhooks/twilio/voice"]) {
    const status = await page.evaluate(async (p) => (await fetch(p)).status, path);
    expect(status, path).toBe(404);
  }
});
