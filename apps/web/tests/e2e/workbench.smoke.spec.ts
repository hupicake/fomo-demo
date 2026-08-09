import { expect, test } from "@playwright/test";

test("home and explicit workbench demo render in Chrome without browser errors", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const liveWorkbenchUrl = process.env.PLAYWRIGHT_WORKBENCH_URL;
  const browserErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const sourceUrl = message.location().url;
    const expectedUnavailableDependency = sourceUrl.endsWith("/favicon.ico")
      || /\/v1\/projects$/.test(sourceUrl)
      || /\/v1\/auth\/me$/.test(sourceUrl);
    if (!expectedUnavailableDependency) browserErrors.push(`${sourceUrl}: ${message.text()}`);
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));

  if (liveWorkbenchUrl) {
    await page.goto(liveWorkbenchUrl);
  } else {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Build software with a team you can inspect." })).toBeVisible();
    await expect(page.locator("[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay")).toHaveCount(0);
    await page.getByRole("link", { name: "Open explicit demo" }).click();
    await expect(page).toHaveURL(/\/projects\/demo-library$/);
  }
  await page.waitForLoadState("networkidle");
  if (!liveWorkbenchUrl) await expect(page.getByText("Explicit demo fixture", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("region", { name: "Agent work log" })).toBeVisible();
  const runStages = page.getByRole("region", { name: "Run stages" });
  const runMetrics = page.getByRole("region", { name: "Run metrics" });
  const taskSummary = page.getByRole("region", { name: "Current task" });
  const composer = page.locator("textarea");
  const workspaceTabs = page.getByRole("navigation", { name: "Workspace tabs" });
  await expect(runStages).toBeVisible();
  await expect(runMetrics).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "Context progress" })).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "Development progress" })).toBeVisible();
  await expect(taskSummary).toBeVisible();
  await expect(page.getByRole("button", { name: "Plan details" })).toBeVisible();
  if (!liveWorkbenchUrl) await expect(page.getByText("All planned work verified", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Agent activity" })).toBeVisible();
  if (!liveWorkbenchUrl) {
    const answeredClarification = page.getByRole("article", { name: "管理工作台优先采用哪种信息密度？" });
    await expect(answeredClarification).toBeVisible();
    await expect(answeredClarification.getByText("Answered", { exact: true })).toBeVisible();
  }
  await expect(page.getByRole("region", { name: "Workspace" })).toBeVisible();
  await expect(workspaceTabs).toBeVisible();
  for (const tab of ["Preview", "Code", "Terminal", "Problems", "Versions"]) {
    await expect(page.getByRole("button", { name: tab, exact: true })).toBeVisible();
  }

  await page.getByRole("button", { name: "Plan details" }).click();
  await expect(page.getByText(/executed goals sequentially/i)).toBeVisible();
  await page.getByRole("button", { name: "Plan details" }).click();

  const viewportLayout = await page.evaluate(() => {
    return {
      documentHeight: document.documentElement.scrollHeight,
      documentWidth: document.documentElement.scrollWidth,
      pageScrollY: window.scrollY,
      viewportHeight: window.innerHeight,
      viewportWidth: window.innerWidth,
    };
  });
  expect(viewportLayout.documentWidth).toBeLessThanOrEqual(viewportLayout.viewportWidth);
  expect(viewportLayout.documentHeight).toBeLessThanOrEqual(viewportLayout.viewportHeight);
  expect(viewportLayout.pageScrollY).toBe(0);

  const fixedLocators = [runStages, runMetrics, taskSummary, composer, workspaceTabs];
  const workLogViewport = page.getByRole("log").locator(":scope > div").first();
  const scrollState = await workLogViewport.evaluate((element) => {
    element.scrollTop = 0;
    return {
      clientHeight: element.clientHeight,
      overflowY: window.getComputedStyle(element).overflowY,
      scrollHeight: element.scrollHeight,
      scrollTop: element.scrollTop,
    };
  });
  expect(scrollState.overflowY).toMatch(/auto|scroll/);
  expect(scrollState.clientHeight).toBeGreaterThan(0);
  expect(scrollState.scrollHeight).toBeGreaterThan(scrollState.clientHeight);

  const beforeScroll = await Promise.all(fixedLocators.map((locator) => locator.boundingBox()));
  beforeScroll.forEach((bounds) => expect(bounds).not.toBeNull());
  const afterScrollTop = await workLogViewport.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
    return element.scrollTop;
  });
  expect(afterScrollTop).toBeGreaterThan(scrollState.scrollTop);
  const afterScroll = await Promise.all(fixedLocators.map((locator) => locator.boundingBox()));
  afterScroll.forEach((bounds) => expect(bounds).not.toBeNull());
  for (let index = 0; index < fixedLocators.length; index += 1) {
    const before = beforeScroll[index];
    const after = afterScroll[index];
    if (!before || !after) continue;
    expect(Math.abs(after.x - before.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(after.y - before.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(after.width - before.width)).toBeLessThanOrEqual(1);
    expect(Math.abs(after.height - before.height)).toBeLessThanOrEqual(1);
  }

  await page.getByRole("button", { name: "Code", exact: true }).click();
  await expect(page.getByRole("tree")).toBeVisible();
  await expect(page.getByText("app/page.tsx", { exact: true }).last()).toBeVisible();
  await expect(page.getByText("Loading editor…", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Terminal", exact: true }).click();
  await expect(page.getByText(/pnpm typecheck/).first()).toBeVisible();
  await page.getByRole("button", { name: "Problems", exact: true }).click();
  await expect(page.getByText("TypeScript typecheck", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Versions", exact: true }).click();
  await expect(page.getByText("Version history", { exact: true })).toBeVisible();
  await expect(page.locator("[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay")).toHaveCount(0);
  expect(browserErrors).toEqual([]);
});
