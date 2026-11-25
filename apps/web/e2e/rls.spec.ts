import { expect, test } from "@playwright/test";
import { signIn, USERS } from "./helpers";

test("the RLS page runs a read-only scan and shows results by service", async ({ page }) => {
  await signIn(page, USERS.platformOperator, { returnTo: "/platform/security/rls" });
  await expect(page.getByRole("heading", { name: "RLS coverage" })).toBeVisible();
  await page.getByTestId("run-rls-scan").click();
  await expect(page.getByTestId("rls-summary")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("tenant-service").first()).toBeVisible();

  // Show all tables: tenant-service's tenant-owned tables are covered, platform tables are marked as such.
  await page.getByLabel("Show only tables with issues").uncheck();
  await expect(page.getByText("public.memberships")).toBeVisible();
  await expect(page.getByText("Platform scoped").first()).toBeVisible();

  // Nothing on this page can change or disable a policy.
  for (const name of [/disable/i, /edit polic/i, /bypass/i, /execute sql/i]) {
    await expect(page.getByRole("button", { name })).toHaveCount(0);
  }
});
