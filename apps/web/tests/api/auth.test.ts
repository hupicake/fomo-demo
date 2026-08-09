import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiProblem, auth } from "@/lib/api/client";

const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
const originalFetch = globalThis.fetch;

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}

function installFetch(...responses: Response[]) {
  const mockedFetch = vi.fn<typeof fetch>();
  for (const response of responses) mockedFetch.mockResolvedValueOnce(response);
  vi.stubGlobal("fetch", mockedFetch);
  return mockedFetch;
}

function requestUrl(fetchMock: ReturnType<typeof installFetch>, call = 0): URL {
  return new URL(String(fetchMock.mock.calls[call]?.[0]));
}

function requestBody(fetchMock: ReturnType<typeof installFetch>, call = 0): Record<string, unknown> {
  return JSON.parse(String(fetchMock.mock.calls[call]?.[1]?.body));
}

// Real backend response shapes, verified against a live HTTP 201 / 200.
const testUser = {
  id: "user_1",
  email: "ada@example.test",
  displayName: "Ada",
  createdAt: "2026-08-10T00:00:00.000Z",
};

// Register / login wrap the user under a nested `user` object.
const testSession = {
  sessionId: "sess_1",
  expiresAt: "2026-09-01T00:00:00.000Z",
  user: testUser,
};

afterEach(() => {
  vi.unstubAllGlobals();
  globalThis.fetch = originalFetch;
  if (originalApiUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL;
  else process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
});

describe("auth contract", () => {
  it("registers against /v1/auth/register and returns the session shape", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse(testSession, 201));

    const session = await auth.register({ email: testUser.email, password: "password123", displayName: "Ada" });

    expect(requestUrl(fetchMock).pathname).toBe("/v1/auth/register");
    expect(requestBody(fetchMock)).toEqual({
      email: testUser.email,
      password: "password123",
      displayName: "Ada",
    });
    // Exact real response shape: nested user, top-level session fields.
    expect(session).toEqual(testSession);
    expect(session.user.id).toBe("user_1");
    expect(session.sessionId).toBe("sess_1");
  });

  it("surfaces a duplicate-email 409 from register as an ApiProblem", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({ detail: "Email already registered" }, 409));

    await expect(auth.register({ email: testUser.email, password: "password123" }))
      .rejects.toMatchObject({ status: 409 });
  });

  it("logs in against /v1/auth/login and returns the session shape", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse(testSession, 200));

    const session = await auth.login({ email: testUser.email, password: "password123" });

    expect(requestUrl(fetchMock).pathname).toBe("/v1/auth/login");
    expect(requestBody(fetchMock)).toEqual({ email: testUser.email, password: "password123" });
    expect(session).toEqual(testSession);
    expect(session.user.id).toBe("user_1");
  });

  it("surfaces invalid-credential 401 from login as an ApiProblem", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({ detail: "Invalid credentials" }, 401));

    await expect(auth.login({ email: testUser.email, password: "wrong" }))
      .rejects.toMatchObject({ status: 401 });
  });

  it("returns the user on a 200 from /auth/me", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse(testUser, 200));

    const user = await auth.me();

    expect(requestUrl(fetchMock).pathname).toBe("/v1/auth/me");
    // GET /auth/me returns the user object directly (no envelope).
    expect(user).toEqual(testUser);
  });

  it("returns null for a guest on a 401 from /auth/me and never bootstraps a guest", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(jsonResponse({ detail: "guest session required" }, 401));

    const user = await auth.me();

    expect(user).toBeNull();
    // Critical contract: a 401 from /auth/me must NOT trigger the generic
    // guest-session bootstrap-and-retry path.
    expect(fetchMock.mock.calls).toHaveLength(1);
    expect(requestUrl(fetchMock).pathname).toBe("/v1/auth/me");
  });

  it("resolves logout against /v1/auth/logout and makes exactly one call", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    const fetchMock = installFetch(new Response(null, { status: 204 }));

    await auth.logout();

    expect(fetchMock.mock.calls).toHaveLength(1);
    expect(requestUrl(fetchMock).pathname).toBe("/v1/auth/logout");
  });

  it("exposes ApiProblem for auth errors so the UI can show clear messages", async () => {
    process.env.NEXT_PUBLIC_API_URL = "https://api.example.test";
    installFetch(jsonResponse({ detail: "Malformed request" }, 422));

    try {
      await auth.login({ email: "x", password: "short" });
      expect.unreachable("login should have thrown");
    } catch (failure) {
      expect(failure).toBeInstanceOf(ApiProblem);
      expect((failure as ApiProblem).status).toBe(422);
    }
  });
});
