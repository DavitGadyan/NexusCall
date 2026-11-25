import { expect, test } from "@playwright/test";
import { signIn, TENANTS, USERS } from "./helpers";

test("a member of two tenants switches between them; data follows the session's tenant", async ({ page }) => {
  await signIn(page, USERS.multi, { returnTo: "/app" });
  const switcher = page.getByTestId("tenant-switcher");
  await expect(switcher).toBeVisible();

  const before = await page.evaluate(async () => (await fetch("/api/bff/api/v1/me/access")).json());

  const options = await switcher.locator("option").allTextContents();
  expect(options).toEqual(expect.arrayContaining([TENANTS.acme, TENANTS.globex]));
  const current = await switcher.locator("option:checked").textContent();
  const next = current === TENANTS.acme ? TENANTS.globex : TENANTS.acme;

  await switcher.selectOption({ label: next });
  await page.waitForURL(/\/app$/);
  await expect(page.getByTestId("tenant-switcher").locator("option:checked")).toHaveText(next);

  const after = await page.evaluate(async () => (await fetch("/api/bff/api/v1/me/access")).json());
  expect(after.tenant_id).not.toBe(before.tenant_id);
  // multi@both.test is an analyst in Acme and a tenant admin in Globex.
  expect(after.roles).not.toEqual(before.roles);
});

test("switching to a tenant the user does not belong to is refused", async ({ page }) => {
  await signIn(page, USERS.analyst, { returnTo: "/app" });
  const status = await page.evaluate(async () => {
    const csrf = document.cookie.split("; ").find((c) => c.startsWith("nx_csrf=") || c.startsWith("__Host-nx_csrf="))?.split("=")[1] ?? "";
    const r = await fetch("/api/session/tenant", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": decodeURIComponent(csrf) },
      body: JSON.stringify({ tenant_id: "0a000000-0000-4000-8000-000000000002" }),
    });
    return r.status;
  });
  expect(status).toBe(403);
});
