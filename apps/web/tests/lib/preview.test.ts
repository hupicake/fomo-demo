import { describe, expect, it } from "vitest";

import { validatePreviewUrl } from "@/lib/preview";

describe("validatePreviewUrl", () => {
  it("accepts an https URL for any host and derives href and origin from the same object", () => {
    const result = validatePreviewUrl("https://preview.example.test/app?tab=books");

    expect(result).toEqual({
      href: "https://preview.example.test/app?tab=books",
      expectedOrigin: "https://preview.example.test",
    });
  });

  it("accepts an https URL with an explicit port", () => {
    expect(validatePreviewUrl("https://preview.example.test:8443/app")).toEqual({
      href: "https://preview.example.test:8443/app",
      expectedOrigin: "https://preview.example.test:8443",
    });
  });

  it("accepts http only for localhost and 127.0.0.1", () => {
    expect(validatePreviewUrl("http://localhost:3000/app")).toEqual({
      href: "http://localhost:3000/app",
      expectedOrigin: "http://localhost:3000",
    });
    expect(validatePreviewUrl("http://127.0.0.1:8080/")).toEqual({
      href: "http://127.0.0.1:8080/",
      expectedOrigin: "http://127.0.0.1:8080",
    });
    expect(validatePreviewUrl("http://LOCALHOST:3000/app")).toEqual({
      href: "http://localhost:3000/app",
      expectedOrigin: "http://localhost:3000",
    });
  });

  it("rejects relative URLs and missing or empty values", () => {
    expect(validatePreviewUrl("/relative/path")).toBeUndefined();
    expect(validatePreviewUrl("app/page")).toBeUndefined();
    expect(validatePreviewUrl("")).toBeUndefined();
    expect(validatePreviewUrl(undefined)).toBeUndefined();
    expect(validatePreviewUrl(null)).toBeUndefined();
  });

  it("rejects every non-http(s) scheme", () => {
    expect(validatePreviewUrl("javascript:alert(1)")).toBeUndefined();
    expect(validatePreviewUrl("ftp://preview.example.test/app")).toBeUndefined();
    expect(validatePreviewUrl("file:///etc/passwd")).toBeUndefined();
    expect(validatePreviewUrl("data:text/html,<p>x</p>")).toBeUndefined();
  });

  it("rejects URL userinfo", () => {
    expect(validatePreviewUrl("https://user:pass@preview.example.test/app")).toBeUndefined();
    expect(validatePreviewUrl("http://user@localhost:3000/app")).toBeUndefined();
  });

  it("rejects non-local http while keeping https for the same host", () => {
    expect(validatePreviewUrl("http://preview.example.test/app")).toBeUndefined();
    expect(validatePreviewUrl("https://preview.example.test/app")).toBeDefined();
  });
});
