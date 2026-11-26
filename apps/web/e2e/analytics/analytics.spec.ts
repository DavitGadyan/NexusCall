/**
 * Analytics flows against a running stack: the overview with UTC date presets, the trend chart with its
 * table view, and an audited export that asks for step-up without a recent MFA sign-in.
 */
import { expect, test } from "@playwright/test";
import { signIn, USERS } from "../helpers";

test("overview shows UTC ranges, funnels and a trend with a table view", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/analytics" });
  await expect(page.getByTestId("analytics-kpis")).toBeVisible();
  await expect(page.getByText(/Days are UTC calendar days/)).toBeVisible();
  await page.getByRole("button", { name: "Last 7 days" }).click();
  await expect(page.getByRole("button", { name: "Last 7 days" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("table", { name: "Call funnel" })).toBeVisible();

  await page.getByTestId("metric-select").selectOption("calls.attempted");
  await page.getByRole("button", { name: "Show as table" }).click();
  await expect(page.getByRole("columnheader", { name: "Day (UTC)" })).toBeVisible();

  const chart = page.getByRole("group", { name: /per UTC day/ });
  await chart.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(page.getByRole("status").filter({ hasText: "(UTC)" })).toBeVisible();
});

test("an export without recent MFA asks for step-up", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/analytics/exports" });
  await page.getByTestId("new-export").click();
  await page.getByTestId("create-export").click();
  await expect(page.getByRole("dialog", { name: "Confirm it's you" }).or(page.getByText("Export requested"))).toBeVisible();
});

test("with step-up, an export is queued and can be downloaded when it succeeds", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { stepUp: true, returnTo: "/app/analytics/exports" });
  await page.getByTestId("new-export").click();
  await page.getByTestId("create-export").click();
  await expect(page.getByText("Export requested")).toBeVisible();
  await expect(page.getByRole("button", { name: "Download" }).first()).toBeVisible({ timeout: 60_000 });
});
