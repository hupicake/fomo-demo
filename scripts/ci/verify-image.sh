#!/bin/sh
set -eu

target=${1:?usage: verify-image.sh <target> <image> [revision] [web-api-url]}
image=${2:?usage: verify-image.sh <target> <image> [revision] [web-api-url]}
revision=${3:-}
web_api_url=${4:-}

case "$target" in
  control-plane|sandbox|web) ;;
  *) echo "Unsupported image target: $target" >&2; exit 1 ;;
esac

architecture=$(docker image inspect -f '{{.Architecture}}' "$image")
[ "$architecture" = amd64 ] || {
  echo "Unexpected image architecture for $target: $architecture" >&2
  exit 1
}

actual_target=$(docker image inspect -f '{{ index .Config.Labels "io.fomo.image.target" }}' "$image")
[ "$actual_target" = "$target" ] || {
  echo "Unexpected image target label for $target: $actual_target" >&2
  exit 1
}

if [ -n "$revision" ]; then
  actual_revision=$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")
  [ "$actual_revision" = "$revision" ] || {
    echo "Unexpected image revision for $target: $actual_revision" >&2
    exit 1
  }
fi

case "$target" in
  control-plane)
    [ "$(docker image inspect -f '{{.Config.User}}' "$image")" = fomo ]
    docker run --rm --platform linux/amd64 --entrypoint python "$image" -c '
import importlib
import os
import shutil

for command in (
    "fomo-api",
    "fomo-migrate",
    "fomo-preview-gateway",
    "fomo-runtime-preflight",
    "fomo-worker",
):
    assert shutil.which(command), command
for module in (
    "fomo.api.app",
    "fomo.persistence.database",
    "fomo.preview_gateway",
    "fomo.runtime_preflight",
    "fomo.worker.runner",
):
    importlib.import_module(module)
assert os.path.isfile("/app/alembic.ini")
assert os.path.isdir("/app/migrations/versions")
assert os.path.isfile("/app/src/fomo/starter_assets/fomo-next-radix-v2/base/package.json")
'
    ;;
  sandbox)
    [ "$(docker image inspect -f '{{.Config.User}}' "$image")" = node ]
    docker run --rm --platform linux/amd64 --entrypoint sh "$image" -ec '
      test "$(id -u)" = 1000
      node --version >/dev/null
      pnpm --version >/dev/null
      pi --version >/dev/null
      opencode --version >/dev/null
      codex --version >/dev/null
      git --version >/dev/null
      rg --version >/dev/null
      test -x /opt/fomo/bin/fomo-pi-rpc-bridge.mjs
      test -x /opt/fomo/bin/fomo-opencode-rpc-bridge.mjs
      test -x /opt/fomo/bin/fomo-codex-rpc-bridge.mjs
      test ! -w /opt/fomo/bin/fomo-pi-rpc-bridge.mjs
      test -x /opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/.bin/next
      test -x /opt/fomo/runtime-cache/fomo-next-radix-v2/node_modules/.bin/playwright
      test -s /opt/fomo/starters/fomo-next-radix-v2/base/package.json
      test -L /opt/fomo/starters/fomo-next-radix-v2/base/node_modules
      find /ms-playwright -maxdepth 1 -type d -name "chromium-*" | grep -q .
    '
    ;;
  web)
    [ "$(docker image inspect -f '{{.Config.User}}' "$image")" = node ]
    actual_web_api_url=$(docker image inspect -f '{{ index .Config.Labels "io.fomo.web.api-url" }}' "$image")
    if [ -n "$web_api_url" ] && [ "$actual_web_api_url" != "$web_api_url" ]; then
      echo "Unexpected Web API build origin: $actual_web_api_url" >&2
      exit 1
    fi
    case "$actual_web_api_url" in
      https://*|http://localhost:*) ;;
      *) echo "Invalid Web API build origin: $actual_web_api_url" >&2; exit 1 ;;
    esac

    container="fomo-image-verify-web-$$"
    cleanup_web() {
      docker rm -f "$container" >/dev/null 2>&1 || true
    }
    on_web_exit() {
      status=$?
      trap - EXIT HUP INT TERM
      cleanup_web
      exit "$status"
    }
    trap on_web_exit EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    docker run --detach --platform linux/amd64 --name "$container" "$image" >/dev/null
    attempt=1
    while [ "$attempt" -le 30 ]; do
      running=$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)
      if [ "$running" != true ]; then
        docker logs "$container" >&2 || true
        echo 'Web image exited before becoming healthy' >&2
        exit 1
      fi
      if docker exec "$container" node -e \
        "fetch('http://127.0.0.1:3000/login').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))" \
        >/dev/null 2>&1; then
        break
      fi
      if [ "$attempt" -eq 30 ]; then
        docker logs "$container" >&2 || true
        echo 'Web image did not become healthy' >&2
        exit 1
      fi
      sleep 1
      attempt=$((attempt + 1))
    done
    docker exec "$container" sh -ec '! grep -R -F "fomo-dev-password" /app/.next >/dev/null 2>&1'
    cleanup_web
    trap - EXIT HUP INT TERM
    ;;
esac

echo "$target image contract passed: $image"
