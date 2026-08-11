import type { NextConfig } from "next";

import { previewGatewayRewrite } from "./lib/preview-gateway-config";

const nextConfig: NextConfig = {
  devIndicators: false,
  output: "standalone",
  experimental: {
    // The deployment host is intentionally small. Keep production builds from
    // spawning one worker per CPU and exhausting memory/swap.
    cpus: 1,
    optimizePackageImports: ["lucide-react"],
  },
  async rewrites() {
    return [previewGatewayRewrite()];
  },
};

export default nextConfig;
