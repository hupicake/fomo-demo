import { expect, test } from "@playwright/test";

test("signed-out home redirects to a prefilled development login", async ({ page }) => {
  let projectRequests = 0;
  await page.route("**/v1/auth/me", (route) => route.fulfill({
    json: { detail: "authentication required" },
    status: 401,
  }));
  await page.route("**/v1/projects", (route) => {
    projectRequests += 1;
    return route.fulfill({ json: [] });
  });

  await page.goto("/");

  await expect(page).toHaveURL(/\/login\?mode=signin&redirect=%2F$/);
  await expect(page.getByLabel("邮箱", { exact: true })).toHaveValue("dev@fomo.local");
  await expect(page.getByLabel("密码", { exact: true })).toHaveValue("fomo-dev-password");
  await expect(page.getByRole("button", { name: "登录", exact: true })).toBeEnabled();
  expect(projectRequests).toBe(0);
});

test("login rejects a backslash-based cross-origin redirect", async ({ page }) => {
  await page.route("**/v1/auth/me", (route) => route.fulfill({
    json: { detail: "authentication required" },
    status: 401,
  }));

  await page.goto("/login?redirect=%2F%5Cevil.example");

  await expect(page.getByRole("link", { name: "返回首页" })).toHaveAttribute("href", "/");
  expect(new URL(page.url()).origin).toBe("http://localhost:3000");
});

test("authenticated workbench renders from API data without a production demo route", async ({ page }) => {
  const browserErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));

  await page.route("**/v1/**", async (route) => {
    const requestUrl = new URL(route.request().url());
    const path = requestUrl.pathname;
    if (path === "/v1/auth/me") {
      await route.fulfill({ json: { id: "user-smoke", email: "smoke@example.test", displayName: "Smoke", createdAt: "2026-08-10T00:00:00.000Z" } });
      return;
    }
    if (path === "/v1/projects") {
      await route.fulfill({ json: [{ id: "project-smoke", title: "Smoke project", status: "idle" }] });
      return;
    }
    if (path === "/v1/projects/project-smoke") {
      await route.fulfill({ json: {
        project: { id: "project-smoke", title: "Smoke project", status: "idle" },
        messages: [],
        runs: [],
        events: [],
        files: [{ path: "app/page.tsx", hash: "sha-smoke", language: "typescript" }],
        versions: [],
        lastSeq: 0,
      } });
      return;
    }
    if (path === "/v1/projects/project-smoke/files/content") {
      await route.fulfill({ json: { path: "app/page.tsx", hash: "sha-smoke", language: "typescript", content: "export default function Page() { return <main>Smoke</main>; }" } });
      return;
    }
    if (path === "/v1/projects/project-smoke/files") {
      await route.fulfill({ json: { files: [{ path: "app/page.tsx", hash: "sha-smoke", language: "typescript" }] } });
      return;
    }
    if (path === "/v1/projects/project-smoke/versions") {
      await route.fulfill({ json: [] });
      return;
    }
    if (path === "/v1/projects/project-smoke/preview") {
      await route.fulfill({ json: { status: "unavailable" } });
      return;
    }
    await route.fulfill({ json: { detail: `Unhandled smoke route: ${path}` }, status: 404 });
  });

  await page.goto("/projects/project-smoke");

  await expect(page.getByRole("region", { name: "Agent 工作日志" })).toBeVisible();
  await expect(page.getByRole("region", { name: "工作区" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "预览", exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "代码", exact: true }).click();
  await expect(page.getByText("app/page.tsx", { exact: true }).first()).toBeVisible();
  await expect(page.locator("[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay")).toHaveCount(0);
  expect(browserErrors).toEqual([]);
});
