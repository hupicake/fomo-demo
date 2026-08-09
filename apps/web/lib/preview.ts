/**
 * The sole preview-location authority on the web side. A persisted preview URL
 * is parsed exactly once and validated for renderability before the workspace
 * may mount an iframe for it.
 */

export interface ValidPreview {
  href: string;
  expectedOrigin: string;
}

const localHostnames = new Set(["localhost", "127.0.0.1"]);

export function validatePreviewUrl(value: string | null | undefined): ValidPreview | undefined {
  if (!value) {
    return undefined;
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    // Relative URLs and malformed inputs never parse; fail closed.
    return undefined;
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    return undefined;
  }
  if (parsed.username || parsed.password) {
    return undefined;
  }
  if (parsed.protocol === "http:" && !localHostnames.has(parsed.hostname)) {
    return undefined;
  }
  return { href: parsed.href, expectedOrigin: parsed.origin };
}
