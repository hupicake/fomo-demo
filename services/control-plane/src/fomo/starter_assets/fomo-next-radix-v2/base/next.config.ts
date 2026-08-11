import type { NextConfig } from "next";

function previewBasePath(): string | undefined {
  const raw = process.env.FOMO_PREVIEW_BASE_PATH;
  if (raw === undefined || raw === "") return undefined;
  if (
    raw !== raw.trim()
    || raw.length > 512
    || !/^\/(?:[A-Za-z0-9][A-Za-z0-9._~-]*)(?:\/[A-Za-z0-9][A-Za-z0-9._~-]*)*$/.test(raw)
  ) {
    throw new Error("FOMO_PREVIEW_BASE_PATH must be a canonical absolute path without a trailing slash");
  }
  return raw;
}

const nextConfig: NextConfig = {
  output: "standalone",
  // basePath is the Next.js sub-path deployment contract: it prefixes Link,
  // router, RSC and _next requests together. assetPrefix is intentionally not
  // set because it is a CDN-only option and combining both would double-prefix
  // generated assets.
  basePath: previewBasePath(),
  allowedDevOrigins: ["127.0.0.1"],
  experimental: {
    optimizePackageImports: ["lucide-react"],
  },
};

export default nextConfig;
