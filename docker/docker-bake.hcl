variable "FOMO_IMAGE_REPOSITORY" {
  default = ""
}

variable "FOMO_IMAGE_TAG" {
  default = "dev"
}

variable "FOMO_CACHE_REPOSITORY" {
  default = ""
}

variable "FOMO_WEB_API_URL" {
  default = "http://localhost:8000"
}

variable "FOMO_WEB_PREVIEW_GATEWAY_INTERNAL_URL" {
  default = "http://preview-gateway:8001"
}

target "_common" {
  platforms = ["linux/amd64"]
  labels = {
    "org.opencontainers.image.revision" = FOMO_IMAGE_TAG
  }
}

target "control-plane" {
  inherits   = ["_common"]
  context    = "./services/control-plane"
  dockerfile = "Dockerfile"
  labels = {
    "io.fomo.image.target" = "control-plane"
  }
  tags = [
    FOMO_IMAGE_REPOSITORY != "" ? "${FOMO_IMAGE_REPOSITORY}:control-plane-${FOMO_IMAGE_TAG}" : "fomo-local/control-plane:${FOMO_IMAGE_TAG}"
  ]
}

target "sandbox" {
  inherits   = ["_common"]
  context    = "./infra/opensandbox"
  dockerfile = "Dockerfile"
  contexts = {
    fomo-control-plane = "./services/control-plane"
  }
  labels = {
    "io.fomo.image.target" = "sandbox"
  }
  tags = [
    FOMO_IMAGE_REPOSITORY != "" ? "${FOMO_IMAGE_REPOSITORY}:sandbox-${FOMO_IMAGE_TAG}" : "fomo-local/sandbox:${FOMO_IMAGE_TAG}"
  ]
}

target "web" {
  inherits   = ["_common"]
  context    = "./apps/web"
  dockerfile = "Dockerfile"
  args = {
    NEXT_PUBLIC_API_URL              = FOMO_WEB_API_URL
    NEXT_PUBLIC_DEV_ACCOUNT_EMAIL    = ""
    NEXT_PUBLIC_DEV_ACCOUNT_PASSWORD = ""
    PREVIEW_GATEWAY_INTERNAL_URL     = FOMO_WEB_PREVIEW_GATEWAY_INTERNAL_URL
  }
  labels = {
    "io.fomo.image.target" = "web"
    "io.fomo.web.api-url"  = FOMO_WEB_API_URL
  }
  tags = [
    FOMO_IMAGE_REPOSITORY != "" ? "${FOMO_IMAGE_REPOSITORY}:web-${FOMO_IMAGE_TAG}" : "fomo-local/web:${FOMO_IMAGE_TAG}"
  ]
}

group "all" {
  targets = ["control-plane", "sandbox", "web"]
}
