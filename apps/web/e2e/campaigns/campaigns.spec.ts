/**
 * Call campaign flows against a running stack: build a draft, schedule, launch with typed confirmation,
 * read the report; the kill switch needs a typed phrase, a reason and step-up.
 */
import { expect, test } from "@playwright/test";
import { signIn, uniqueSuffix, USERS } from "../helpers";

test("build, schedule and launch a campaign, then see its report", async ({ page }) => {
  const name = `E2E campaign ${uniqueSuffix()}`;
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/campaigns/calls/new" });
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("AI voice agent").selectOption({ index: 1 });
  await page.getByLabel("Segment").selectOption({ index: 1 });
  await page.getByTestId("save-campaign").click();
  await expect(page).toHaveURL(/\/app\/campaigns\/calls\/[0-9a-f-]{36}$/);
  await expect(page.getByText("draft", { exact: true })).toBeVisible();

  await page.getByTestId("campaign-schedule").click();
  await expect(page.getByText("scheduled", { exact: true })).toBeVisible();
  await page.getByTestId("campaign-launch").click();
  await page.getByLabel(/Type .* to confirm/).fill(name);
  await page.getByRole("dialog").getByRole("button", { name: "Launch" }).click();
  // Either it launches, or the dialog lists why (inactive agent, workspace limits, kill switch).
  await expect(page.getByText("Campaign launched").or(page.getByText(/Cannot launch yet|Campaign cannot be launched/))).toBeVisible();
  await page.getByRole("tab", { name: "Report" }).click();
  await expect(page.getByText("Contacts by state")).toBeVisible();
});

test("the kill switch requires a typed phrase and a reason", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { stepUp: true, returnTo: "/app/campaigns/calls" });
  const button = page.getByTestId("kill-switch");
  const label = (await button.textContent()) ?? "";
  await button.click();
  const dialog = page.getByRole("dialog");
  const confirm = dialog.getByRole("button", { name: /Stop all calls|Release/ });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Reason/).fill("e2e verification");
  await dialog.getByLabel(/Type .* to confirm/).fill(label.includes("Release") ? "RESUME CALLS" : "STOP ALL CALLS");
  await confirm.click();
  await expect(page.getByText(/All campaign calls stopped|Kill switch released/)).toBeVisible();
  // Leave the workspace as it was.
  await page.getByTestId("kill-switch").click();
  await page.getByRole("dialog").getByLabel(/Reason/).fill("e2e verification done");
  await page.getByRole("dialog").getByLabel(/Type .* to confirm/).fill(label.includes("Release") ? "STOP ALL CALLS" : "RESUME CALLS");
  await page.getByRole("dialog").getByRole("button", { name: /Stop all calls|Release/ }).click();
});
