#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
cd "$root"

remote=${1:-cnb}
branch=${2:-main}
source_ref=${3:-HEAD}
repository=${FOMO_CNB_REPOSITORY:-hupicake/fomo-demo}
full_event=api_trigger_fomo_build
small_event=api_trigger_fomo_build_small
title_prefix='fomo auto images'
dry_run=${FOMO_CNB_DRY_RUN:-false}

case "$dry_run" in
  true|false) ;;
  *) echo 'FOMO_CNB_DRY_RUN must be true or false' >&2; exit 1 ;;
esac

command -v cnb >/dev/null 2>&1 || {
  echo 'CNB CLI is required' >&2
  exit 1
}
command -v jq >/dev/null 2>&1 || {
  echo 'jq is required' >&2
  exit 1
}

source_commit=$(git rev-parse "$source_ref^{commit}")

# Synchronize exactly the committed revision. Uncommitted workspace changes are
# intentionally excluded from both the remote branch and the image contexts.
sh scripts/ci/cnb-sync.sh "$remote" "$branch" "$source_commit"

history=$(cnb build get-build-logs \
  --repo "$repository" \
  --status success \
  --targetRef "$branch" \
  --page-size 100 \
  --verbose)

printf '%s' "$history" | jq -e '.status == 200 and (.data.data | type == "array")' >/dev/null || {
  echo 'Unable to query successful CNB builds' >&2
  exit 1
}

# CNB reports the aggregate result for the API-triggered build. A revision is
# reusable only when both its build and independent verification pipelines made
# that aggregate build successful.
already_built=$(printf '%s' "$history" | jq -r \
  --arg sha "$source_commit" \
  --arg prefix "$title_prefix " \
  '[.data.data[] | select(.sha == $sha and ((.title // "") | startswith($prefix)))] | length')
if [ "$already_built" -gt 0 ]; then
  echo "CNB revision already built and verified successfully: $source_commit"
  exit 0
fi

baseline=$(
  printf '%s' "$history" | jq -r \
    --arg sha "$source_commit" \
    --arg prefix "$title_prefix " \
    '.data.data[] | select(.sha != "" and .sha != $sha and ((.title // "") | startswith($prefix))) | .sha' |
  while IFS= read -r candidate; do
    if git cat-file -e "$candidate^{commit}" 2>/dev/null && \
      git merge-base --is-ancestor "$candidate" "$source_commit" 2>/dev/null; then
      printf '%s\n' "$candidate"
      break
    fi
  done
)

images_file=$(mktemp)
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -f "$images_file"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "$baseline" ]; then
  CI_COMMIT_SHA="$source_commit" CI_PREV_COMMIT_SHA="$baseline" \
    sh scripts/ci/detect-images.sh "$images_file"
  build_env=$(jq -nc --arg baseline "$baseline" '{FOMO_BASE_SHA: $baseline}')
else
  FOMO_IMAGES=all FOMO_IMAGES_FILE="$images_file" \
    sh scripts/ci/cnb-build.sh select >/dev/null
  build_env='{"FOMO_IMAGES":"all"}'
  echo 'No successful verified CNB baseline; selecting every image target'
fi

if [ ! -s "$images_file" ]; then
  echo "No application image changed since CNB baseline: $baseline"
  exit 0
fi

if grep -qx web "$images_file"; then
  if [ -z "${FOMO_WEB_API_URL:-}" ]; then
    echo 'FOMO_WEB_API_URL is required when building Web' >&2
    exit 1
  fi
  case "$FOMO_WEB_API_URL" in
    https://*) ;;
    *) echo 'FOMO_WEB_API_URL must use HTTPS for a CNB Web image' >&2; exit 1 ;;
  esac
  case "$FOMO_WEB_API_URL" in
    *'@'*|*'?'*|*'#'*)
      echo 'FOMO_WEB_API_URL must be a credential-free HTTPS origin' >&2
      exit 1
      ;;
  esac
  build_env=$(printf '%s' "$build_env" | jq -c \
    --arg web_api_url "$FOMO_WEB_API_URL" \
    '. + {FOMO_WEB_API_URL: $web_api_url}')
fi

images=$(paste -sd, "$images_file")
image_count=$(wc -l < "$images_file" | tr -d ' ')
event=$small_event
profile=small-8c

# Sandbox dependency preparation is the cold-path heavyweight target. It and
# every multi-image build use the larger runner; one Web or control-plane image
# uses the smaller runner.
if [ "$image_count" -gt 1 ] || grep -qx sandbox "$images_file"; then
  event=$full_event
  profile=full-16c
fi

short_sha=$(printf '%.8s' "$source_commit")
title="$title_prefix $short_sha"

printf 'CNB_BUILD_SHA=%s\n' "$source_commit"
printf 'CNB_BUILD_BASELINE=%s\n' "${baseline:-none}"
printf 'CNB_BUILD_IMAGES=%s\n' "$images"
printf 'CNB_BUILD_PROFILE=%s\n' "$profile"
printf 'CNB_BUILD_EVENT=%s\n' "$event"

if [ "$dry_run" = true ]; then
  echo 'CNB build trigger skipped in dry-run mode'
  exit 0
fi

request=$(jq -nc \
  --arg branch "$branch" \
  --arg sha "$source_commit" \
  --arg event "$event" \
  --arg title "$title" \
  --argjson env "$build_env" \
  '{branch: $branch, sha: $sha, event: $event, sync: "true", title: $title, env: $env}')

response=$(cnb build start-build --repo "$repository" --data "$request" --verbose)
printf '%s' "$response" | jq -e '.status == 200 and .data.success == true' >/dev/null || {
  echo 'CNB build trigger failed' >&2
  exit 1
}

printf 'CNB_BUILD_SN=%s\n' "$(printf '%s' "$response" | jq -r '.data.sn')"
printf 'CNB_BUILD_URL=%s\n' "$(printf '%s' "$response" | jq -r '.data.buildLogUrl')"
