import type { NextConfig } from "next";

import { previewGatewayRewrite } from "./lib/preview-gateway-config";

const nextConfig: NextConfig = {
  devIndicators: false,
  output: "standalone",
  experimental: {
    optimizePackageImports: ["lucide-react"],
  },
  async rewrites() {
    return [previewGatewayRewrite()];
  },
};

export default nextConfig;
