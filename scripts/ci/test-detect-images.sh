#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

assert_images() {
  name=$1
  expected=$2
  shift 2
  files=$(jq -nc '$ARGS.positional' --args -- "$@")
  output="$tmp/$name"
  CI_PIPELINE_FILES="$files" sh "$root/scripts/ci/detect-images.sh" "$output" >/dev/null
  actual=$(paste -sd, "$output")
  [ "$actual" = "$expected" ] || {
    echo "$name: expected [$expected], got [$actual]" >&2
    exit 1
  }
}

assert_images application-slices 'control-plane,web' \
  apps/web/app/page.tsx \
  services/control-plane/src/fomo/api/app.py

assert_images starter-coupling 'control-plane,sandbox' \
  services/control-plane/src/fomo/starter_assets/fomo-next-radix-v2/base/package.json

assert_images sandbox-runtime sandbox \
  infra/opensandbox/fomo-codex-rpc-bridge.mjs

assert_images non-image-changes '' \
  apps/web/tests/api/client.test.ts \
  services/control-plane/tests/test_api.py \
  infra/opensandbox/fomo-codex-rpc-bridge.test.mjs \
  compose.yaml \
  docs/agent-runtime-lessons/README.md

assert_images build-contract 'control-plane,sandbox,web' docker/docker-bake.hcl
assert_images unknown-fails-safe 'control-plane,sandbox,web' new-runtime/service.go

forced="$tmp/forced"
FOMO_FORCE_ALL=true sh "$root/scripts/ci/detect-images.sh" "$forced" >/dev/null
[ "$(paste -sd, "$forced")" = 'control-plane,sandbox,web' ]

echo 'CNB image detection tests passed'
