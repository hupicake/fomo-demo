import type { NextConfig } from "next";

function previewAssetPrefix(): string | undefined {
  const raw = process.env.FOMO_PREVIEW_ASSET_PREFIX;
  if (raw === undefined || raw === "") return undefined;
  if (
    raw !== raw.trim()
    || raw.length > 512
    || !/^\/(?:[A-Za-z0-9][A-Za-z0-9._~-]*)(?:\/[A-Za-z0-9][A-Za-z0-9._~-]*)*$/.test(raw)
  ) {
    throw new Error("FOMO_PREVIEW_ASSET_PREFIX must be a canonical absolute path without a trailing slash");
  }
  return raw;
}

const nextConfig: NextConfig = {
  output: "standalone",
  assetPrefix: previewAssetPrefix(),
  allowedDevOrigins: ["127.0.0.1"],
  experimental: {
    optimizePackageImports: ["lucide-react"],
  },
};

export default nextConfig;
