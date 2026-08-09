import { expect, test, type BrowserContext, type Page } from "@playwright/test";

const apiBase = process.env.PLAYWRIGHT_API_URL || "http://localhost:8000/v1";

type SessionResponse = {
  id?: string;
  sessionId?: string;
};

async function createGuestProject(context: BrowserContext, title: string) {
  const guestResponse = await context.request.post(`${apiBase}/sessions/guest`);
  expect(guestResponse.status()).toBe(201);
  const guest = (await guestResponse.json()) as SessionResponse;
  expect(guest.id).toBeTruthy();

  const projectResponse = await context.request.post(`${apiBase}/projects`, {
    data: { title },
    headers: { "X-FOMO-Session": guest.id! },
  });
  expect(projectResponse.status()).toBe(201);
  const project = (await projectResponse.json()) as { id: string; title: string };
  expect(project.title).toBe(title);
  return { guestSessionId: guest.id!, projectId: project.id };
}

async function openAuthDialog(page: Page, action: "Create account" | "Sign in") {
  await page.getByRole("button", { name: "Account and workspace status" }).click();
  await page.getByRole("menuitem", { name: action, exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
}

async function fillCredentials(page: Page, email: string, password: string) {
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
}

test("guest work survives registration while sessions and accounts remain isolated", async ({ browser }) => {
  const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ownerEmail = `owner-${suffix}@example.test`;
  const otherEmail = `other-${suffix}@example.test`;
  const password = `Demo-pass-${suffix}!`;
  const displayName = `Demo Owner ${suffix.slice(-6)}`;
  const projectTitle = `Account isolation ${suffix}`;

  const ownerContext = await browser.newContext();
  const ownerPage = await ownerContext.newPage();
  const { guestSessionId, projectId } = await createGuestProject(ownerContext, projectTitle);

  await ownerPage.goto("/");
  await expect(ownerPage.getByRole("link", { name: new RegExp(projectTitle) })).toBeVisible();

  await openAuthDialog(ownerPage, "Create account");
  await fillCredentials(ownerPage, ownerEmail, password);
  await ownerPage.locator("#auth-display-name").fill(displayName);
  const registerResponsePromise = ownerPage.waitForResponse(
    (response) => response.url() === `${apiBase}/auth/register` && response.request().method() === "POST",
  );
  await ownerPage.getByRole("dialog").getByRole("button", { name: "Create account", exact: true }).click();
  const registerResponse = await registerResponsePromise;
  expect(registerResponse.status()).toBe(201);
  const registered = (await registerResponse.json()) as SessionResponse;
  expect(registered.sessionId).toBeTruthy();
  expect(registered.sessionId).not.toBe(guestSessionId);

  await expect(ownerPage.getByRole("button", { name: `Account: ${displayName}` })).toBeVisible();
  await expect(ownerPage.getByRole("link", { name: new RegExp(projectTitle) })).toBeVisible();

  await ownerPage.getByRole("button", { name: `Account: ${displayName}` }).click();
  const logoutResponsePromise = ownerPage.waitForResponse(
    (response) => response.url() === `${apiBase}/auth/logout` && response.request().method() === "POST",
  );
  await ownerPage.getByRole("menuitem", { name: "Sign out", exact: true }).click();
  expect((await logoutResponsePromise).status()).toBe(204);
  await expect(ownerPage.getByRole("button", { name: "Account and workspace status" })).toBeVisible();
  await expect(ownerPage.getByRole("link", { name: new RegExp(projectTitle) })).toHaveCount(0);

  await openAuthDialog(ownerPage, "Sign in");
  await fillCredentials(ownerPage, ownerEmail, password);
  const loginResponsePromise = ownerPage.waitForResponse(
    (response) => response.url() === `${apiBase}/auth/login` && response.request().method() === "POST",
  );
  await ownerPage.getByRole("dialog").getByRole("button", { name: "Sign in", exact: true }).click();
  expect((await loginResponsePromise).status()).toBe(200);
  await expect(ownerPage.getByRole("button", { name: `Account: ${displayName}` })).toBeVisible();
  await expect(ownerPage.getByRole("link", { name: new RegExp(projectTitle) })).toBeVisible();

  const otherContext = await browser.newContext();
  const otherPage = await otherContext.newPage();
  const otherRegistration = await otherContext.request.post(`${apiBase}/auth/register`, {
    data: { email: otherEmail, password, displayName: "Other evaluator" },
  });
  expect(otherRegistration.status()).toBe(201);
  const otherProjects = await otherContext.request.get(`${apiBase}/projects`);
  expect(otherProjects.status()).toBe(200);
  expect(await otherProjects.json()).toEqual([]);
  const forbiddenProject = await otherContext.request.get(`${apiBase}/projects/${projectId}`);
  expect(forbiddenProject.status()).toBe(403);

  await otherPage.goto(`/projects/${projectId}`);
  await expect(otherPage.getByRole("heading", { name: "Project is temporarily unavailable" })).toBeVisible();

  await otherContext.close();
  await ownerContext.close();
});
