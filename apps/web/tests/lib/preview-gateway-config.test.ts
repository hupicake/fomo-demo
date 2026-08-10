import { describe, expect, it } from "vitest";

import {
  previewGatewayRewrite,
  resolvePreviewGatewayInternalUrl,
} from "@/lib/preview-gateway-config";

describe("preview gateway rewrite", () => {
  it("uses Docker in production, loopback in development, and validates overrides", () => {
    expect(previewGatewayRewrite({ NODE_ENV: "production" })).toEqual({
      source: "/preview/:path*",
      destination: "http://preview-gateway:8001/preview/:path*",
    });
    expect(previewGatewayRewrite({ NODE_ENV: "development" }).destination).toBe(
      "http://127.0.0.1:8001/preview/:path*",
    );
    expect(resolvePreviewGatewayInternalUrl({
      NODE_ENV: "production",
      PREVIEW_GATEWAY_INTERNAL_URL: "https://gateway.internal:8443",
    })).toBe("https://gateway.internal:8443");
    expect(() => resolvePreviewGatewayInternalUrl({
      NODE_ENV: "production",
      PREVIEW_GATEWAY_INTERNAL_URL: "http://user:secret@gateway.internal",
    })).toThrow("absolute HTTP(S) origin");
  });
});
