const developmentGateway = "http://127.0.0.1:8001";
const productionGateway = "http://preview-gateway:8001";

type PreviewGatewayEnvironment = {
  NODE_ENV?: string;
  PREVIEW_GATEWAY_INTERNAL_URL?: string;
};

export function resolvePreviewGatewayInternalUrl(
  environment: PreviewGatewayEnvironment = process.env,
): string {
  const configured = environment.PREVIEW_GATEWAY_INTERNAL_URL?.trim();
  const candidate = configured
    || (environment.NODE_ENV === "production" ? productionGateway : developmentGateway);
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error("PREVIEW_GATEWAY_INTERNAL_URL must be an absolute HTTP(S) origin");
  }
  if (
    !["http:", "https:"].includes(parsed.protocol)
    || parsed.username
    || parsed.password
    || parsed.pathname !== "/"
    || parsed.search
    || parsed.hash
  ) {
    throw new Error("PREVIEW_GATEWAY_INTERNAL_URL must be an absolute HTTP(S) origin");
  }
  return parsed.origin;
}

export function previewGatewayRewrite(
  environment: PreviewGatewayEnvironment = process.env,
) {
  return {
    source: "/preview/:path*",
    destination: `${resolvePreviewGatewayInternalUrl(environment)}/preview/:path*`,
  } as const;
}
