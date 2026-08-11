#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
cd "$root"

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

grep -q '^"\*\*":' .cnb.yml || fail 'CNB branch wildcard is missing'
grep -q '^  api_trigger_fomo_build:' .cnb.yml || fail 'CNB full API trigger is missing'
grep -q '^  api_trigger_fomo_build_small:' .cnb.yml || fail 'CNB small API trigger is missing'
grep -q 'cpus: 16' .cnb.yml || fail 'CNB 16 CPU runner is missing'
grep -q 'cpus: 8' .cnb.yml || fail 'CNB 8 CPU runner is missing'
grep -q 'cpus: 4' .cnb.yml || fail 'CNB 4 CPU verifier is missing'
grep -q 'type: cnb:resolve' .cnb.yml || fail 'CNB build dependency release is missing'
grep -q 'type: cnb:await' .cnb.yml || fail 'CNB verifier dependency wait is missing'
grep -q 'rootlessBuildkitd:' .cnb.yml || fail 'CNB Rootless BuildKit is missing'
grep -q 'sh scripts/ci/cnb-build.sh build' .cnb.yml || fail 'CNB build stage is missing'
grep -q 'sh scripts/ci/cnb-build.sh verify' .cnb.yml || fail 'CNB verify stage is missing'

if grep -Eiq 'deploy|docker[[:space:]]+compose|(^|[[:space:]])ssh([[:space:]]|$)' .cnb.yml; then
  fail 'CNB pipeline must not access production deployment mechanisms'
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

FOMO_IMAGES=web,control-plane FOMO_IMAGES_FILE="$tmp/selected" \
  sh scripts/ci/cnb-build.sh select >/dev/null
[ "$(paste -sd, "$tmp/selected")" = 'control-plane,web' ] || fail 'manual selection is not canonical'

FOMO_IMAGES=all FOMO_IMAGES_FILE="$tmp/all" \
  sh scripts/ci/cnb-build.sh select >/dev/null
[ "$(wc -l < "$tmp/all" | tr -d ' ')" = 3 ] || fail 'full selection must contain three targets'

if FOMO_IMAGES=unknown FOMO_IMAGES_FILE="$tmp/invalid" \
  sh scripts/ci/cnb-build.sh select >/dev/null 2>&1; then
  fail 'invalid image selection was accepted'
fi

FOMO_IMAGE_TAG=test-contract \
FOMO_WEB_API_URL=https://app.example.test \
FOMO_CACHE_REPOSITORY=registry.example.test/fomo \
  docker buildx bake \
    --file docker/docker-bake.hcl \
    --file docker/docker-bake.cnb.hcl \
    --print all > "$tmp/bake.json"

jq -e '
  (.target | keys | sort) == ["control-plane", "sandbox", "web"] and
  .target.sandbox.contexts["fomo-control-plane"] == "./services/control-plane" and
  .target.web.args.NEXT_PUBLIC_API_URL == "https://app.example.test" and
  .target.web.args.NEXT_PUBLIC_DEV_ACCOUNT_EMAIL == "" and
  .target.web.args.NEXT_PUBLIC_DEV_ACCOUNT_PASSWORD == "" and
  ([.target[] | .platforms == ["linux/amd64"]] | all) and
  ([.target[] | .labels["org.opencontainers.image.revision"] == "test-contract"] | all) and
  ([.target[] | (."cache-from" | length) == 1 and (."cache-to" | length) == 1] | all)
' "$tmp/bake.json" >/dev/null || fail 'resolved Bake contract is invalid'

echo 'CNB build contract tests passed'
