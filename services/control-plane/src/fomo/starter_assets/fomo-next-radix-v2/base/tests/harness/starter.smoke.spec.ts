import { expect, test } from "@playwright/test";

test("starter renders a stable application shell", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect(page.getByRole("main")).toBeVisible();
  expect(pageErrors).toEqual([]);
});
