/** Bookings calendar, list and resources against a RUNNING local stack (never started here). */
import { expect, test } from "@playwright/test";
import { signIn, uniqueSuffix, USERS } from "../helpers";

test("calendar switches between week and day and the list filters", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/bookings" });
  await expect(page.getByRole("heading", { name: "Bookings" })).toBeVisible();
  await page.getByRole("button", { name: "day", exact: true }).click();
  await page.getByRole("button", { name: "Next day" }).click();
  await page.getByRole("button", { name: "week", exact: true }).click();
  await page.getByRole("tab", { name: "List" }).click();
  await page.getByLabel("Status").selectOption("confirmed");
  await expect(page.getByRole("table", { name: "Bookings" }).or(page.getByText("No bookings match"))).toBeVisible();
});

test("a resource gets weekly hours, then a booking is created from a free slot", async ({ page }) => {
  await signIn(page, USERS.acmeOwner, { returnTo: "/app/bookings/resources" });
  const name = `E2E room ${uniqueSuffix()}`;
  await page.getByRole("button", { name: "Add resource" }).click();
  await page.getByRole("dialog").getByLabel("Name").fill(name);
  await page.getByRole("dialog").getByLabel("Time zone").fill("UTC");
  await page.getByRole("dialog").getByRole("button", { name: "Save" }).click();
  const row = page.getByRole("row", { name: new RegExp(name) });
  await row.getByRole("button", { name: "Edit hours" }).click();
  await page.getByRole("button", { name: "Add Mon–Fri 09:00–17:00" }).click();
  await page.getByRole("button", { name: "Save hours" }).click();
  await expect(page.getByText("Weekly hours saved")).toBeVisible();

  await page.goto("/app/bookings");
  await page.getByTestId("new-booking").click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Contact").fill("ac");
  const contact = dialog.getByRole("button").filter({ hasNotText: /Cancel|Create booking|Close/ }).first();
  test.skip((await contact.count()) === 0, "no contacts in the demo data");
  await contact.click();
  await dialog.getByLabel("Resource").selectOption({ label: `${name} (UTC)` });
  // next weekday
  const d = new Date();
  do d.setDate(d.getDate() + 1);
  while (d.getDay() === 0 || d.getDay() === 6);
  await dialog.getByLabel("Day").fill(d.toISOString().slice(0, 10));
  const slot = dialog.getByTestId("slots").getByRole("option").first();
  await slot.click();
  await dialog.getByTestId("save-booking").click();
  await expect(page.getByText("Booking created")).toBeVisible();
});
