import { expect, test, type Page } from "@playwright/test";

const apiBase = process.env.PLAYWRIGHT_API_URL || "http://localhost:8000/v1";

async function fillCredentials(page: Page, email: string, password: string) {
  await page.getByLabel("邮箱", { exact: true }).fill(email);
  await page.getByLabel("密码", { exact: true }).fill(password);
}

test("registered accounts keep projects isolated across logout and login", async ({ browser }) => {
  const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ownerEmail = `owner-${suffix}@example.test`;
  const otherEmail = `other-${suffix}@example.test`;
  const password = `Demo-pass-${suffix}!`;
  const displayName = `Owner ${suffix.slice(-6)}`;
  const projectTitle = `Account isolation ${suffix}`;

  const ownerContext = await browser.newContext();
  const ownerPage = await ownerContext.newPage();
  await ownerPage.goto("/");

  await expect(ownerPage).toHaveURL(/\/login\?mode=signin&redirect=%2F$/);
  await ownerPage.getByRole("button", { name: "注册一个", exact: true }).click();
  await fillCredentials(ownerPage, ownerEmail, password);
  await ownerPage.getByLabel("显示名称", { exact: false }).fill(displayName);
  const registerResponsePromise = ownerPage.waitForResponse(
    (response) => response.url() === `${apiBase}/auth/register` && response.request().method() === "POST",
  );
  await ownerPage.getByRole("button", { name: "创建账号", exact: true }).click();
  const registerResponse = await registerResponsePromise;
  expect(registerResponse.status()).toBe(201);
  expect(await registerResponse.json()).not.toHaveProperty("sessionId");
  await expect(ownerPage).toHaveURL("/");
  await expect(ownerPage.getByRole("button", { name: `账号：${displayName}` })).toBeVisible();

  const projectResponse = await ownerContext.request.post(`${apiBase}/projects`, {
    data: { title: projectTitle },
  });
  expect(projectResponse.status()).toBe(201);
  const project = (await projectResponse.json()) as { id: string; title: string };
  expect(project.title).toBe(projectTitle);
  await ownerPage.reload();
  await expect(ownerPage.getByRole("link", { name: new RegExp(projectTitle) })).toBeVisible();

  await ownerPage.getByRole("button", { name: `账号：${displayName}` }).click();
  const logoutResponsePromise = ownerPage.waitForResponse(
    (response) => response.url() === `${apiBase}/auth/logout` && response.request().method() === "POST",
  );
  await ownerPage.getByRole("menuitem", { name: "退出登录", exact: true }).click();
  expect((await logoutResponsePromise).status()).toBe(204);
  await expect(ownerPage).toHaveURL(/\/login\?mode=signin&redirect=%2F$/);
  await fillCredentials(ownerPage, ownerEmail, password);
  const loginResponsePromise = ownerPage.waitForResponse(
    (response) => response.url() === `${apiBase}/auth/login` && response.request().method() === "POST",
  );
  await ownerPage.getByRole("button", { name: "登录", exact: true }).click();
  expect((await loginResponsePromise).status()).toBe(200);
  await expect(ownerPage).toHaveURL("/");
  await expect(ownerPage.getByRole("link", { name: new RegExp(projectTitle) })).toBeVisible();

  const otherContext = await browser.newContext();
  const otherRegistration = await otherContext.request.post(`${apiBase}/auth/register`, {
    data: { email: otherEmail, password, displayName: "Other evaluator" },
  });
  expect(otherRegistration.status()).toBe(201);
  expect(await otherRegistration.json()).not.toHaveProperty("sessionId");
  const otherProjects = await otherContext.request.get(`${apiBase}/projects`);
  expect(otherProjects.status()).toBe(200);
  expect(await otherProjects.json()).toEqual([]);
  const forbiddenProject = await otherContext.request.get(`${apiBase}/projects/${project.id}`);
  expect(forbiddenProject.status()).toBe(403);

  const otherPage = await otherContext.newPage();
  await otherPage.goto(`/projects/${project.id}`);
  await expect(otherPage.getByRole("heading", { name: "项目暂时不可用" })).toBeVisible();

  await otherContext.close();
  await ownerContext.close();
});
