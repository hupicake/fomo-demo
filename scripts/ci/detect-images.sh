#!/bin/sh
set -eu

output=${1:-.ci/images}
mkdir -p "$(dirname "$output")"
files=$(mktemp)
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -f "$files"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

commit=${CI_COMMIT_SHA:-HEAD}
previous=${CI_PREV_COMMIT_SHA:-}
pipeline_files=${CI_PIPELINE_FILES:-}
force_all=${FOMO_FORCE_ALL:-false}

if [ "$force_all" = true ]; then
  printf '%s\n' docker/docker-bake.hcl > "$files"
elif [ -n "$pipeline_files" ]; then
  printf '%s' "$pipeline_files" | jq -r '.[]' > "$files"
elif [ -n "$previous" ] && \
  [ "$previous" != "$commit" ] && \
  git cat-file -e "$previous^{commit}" 2>/dev/null && \
  git merge-base --is-ancestor "$previous" "$commit" 2>/dev/null; then
  # Disable rename detection so deleting a runtime path still selects its old
  # image even when Git recognizes the destination as documentation or tests.
  git diff --no-renames --name-only "$previous" "$commit" > "$files"
elif git rev-parse "$commit^" >/dev/null 2>&1; then
  git diff --no-renames --name-only "$commit^" "$commit" > "$files"
else
  # Missing or truncated change metadata must fail safe to a full image build.
  printf '%s\n' docker/docker-bake.hcl > "$files"
fi

images=""
add_image() {
  case " $images " in
    *" $1 "*) ;;
    *) images="$images $1" ;;
  esac
}

add_all() {
  add_image control-plane
  add_image sandbox
  add_image web
}

while IFS= read -r file; do
  case "$file" in
    '') ;;
    .cnb.yml|scripts/ci/*)
      # Pipeline orchestration does not alter application image contents.
      ;;
    docker/docker-bake*.hcl)
      add_all
      ;;
    apps/web/tests/*|apps/web/playwright.local.config.ts|apps/web/vitest.config.ts|apps/web/AGENTS.md)
      ;;
    apps/web/*)
      add_image web
      ;;
    services/control-plane/tests/*)
      ;;
    services/control-plane/src/fomo/starter_assets/*)
      add_image control-plane
      add_image sandbox
      ;;
    services/control-plane/*)
      add_image control-plane
      ;;
    infra/opensandbox/*.test.mjs|infra/opensandbox/config.toml)
      ;;
    infra/opensandbox/*)
      add_image sandbox
      ;;
    AGENTS.md|README.md|SUBMISSION.md|.gitignore|docs/*|deploy/*|infra/litellm/*|compose.yaml|scripts/dev-up.sh)
      ;;
    *)
      # New top-level runtime paths must not silently bypass image publication.
      add_all
      ;;
  esac
done < "$files"

: > "$output"
for image in control-plane sandbox web; do
  case " $images " in
    *" $image "*) printf '%s\n' "$image" >> "$output" ;;
  esac
done

printf 'changed files: %s\n' "$(wc -l < "$files" | tr -d ' ')"
if [ -s "$output" ]; then
  printf 'affected images: %s\n' "$(paste -sd, "$output")"
else
  echo 'affected images: none'
fi
