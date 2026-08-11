#!/bin/sh
set -eu

mode=${1:-all}
images_file=${FOMO_IMAGES_FILE:-.ci/cnb-images}
selection=${FOMO_IMAGES:-auto}
all_images='control-plane sandbox web'
verify_parallelism=${FOMO_VERIFY_PARALLELISM:-3}

write_all_images() {
  : > "$images_file"
  for image in $all_images; do
    printf '%s\n' "$image" >> "$images_file"
  done
}

select_images() {
  mkdir -p "$(dirname "$images_file")"

  case "$selection" in
    ''|auto)
      commit=${CNB_COMMIT:-${CI_COMMIT_SHA:-HEAD}}
      previous=${FOMO_BASE_SHA:-${CNB_BEFORE_SHA:-${CI_PREV_COMMIT_SHA:-}}}
      if [ -n "$previous" ] && \
        [ "$previous" != 0000000000000000000000000000000000000000 ] && \
        git cat-file -e "$previous^{commit}" 2>/dev/null && \
        git merge-base --is-ancestor "$previous" "$commit" 2>/dev/null; then
        CI_COMMIT_SHA="$commit" CI_PREV_COMMIT_SHA="$previous" \
          sh scripts/ci/detect-images.sh "$images_file"
      else
        echo 'No valid CNB build baseline; selecting every image target'
        write_all_images
      fi
      ;;
    all)
      write_all_images
      ;;
    *)
      requested=$(mktemp)
      trap 'rm -f "$requested"' EXIT HUP INT TERM
      printf '%s\n' "$selection" | tr ',' '\n' | tr -d ' \t\r' | sed '/^$/d' > "$requested"
      [ -s "$requested" ] || {
        echo 'FOMO_IMAGES contains no image target' >&2
        exit 1
      }

      while IFS= read -r image; do
        case " $all_images " in
          *" $image "*) ;;
          *) echo "Unsupported image target: $image" >&2; exit 1 ;;
        esac
      done < "$requested"

      : > "$images_file"
      for image in $all_images; do
        if grep -qx "$image" "$requested"; then
          printf '%s\n' "$image" >> "$images_file"
        fi
      done
      rm -f "$requested"
      trap - EXIT HUP INT TERM
      ;;
  esac

  if [ -s "$images_file" ]; then
    printf 'CNB image targets: %s\n' "$(paste -sd, "$images_file")"
  else
    echo 'CNB image targets: none'
  fi
}

validate_web_contract() {
  grep -qx web "$images_file" || return 0
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
}

configure_registry() {
  : "${CNB_DOCKER_REGISTRY:?CNB_DOCKER_REGISTRY is required}"
  : "${CNB_REPO_SLUG_LOWERCASE:?CNB_REPO_SLUG_LOWERCASE is required}"

  FOMO_IMAGE_REPOSITORY=${FOMO_IMAGE_REPOSITORY:-${CNB_DOCKER_REGISTRY}/${CNB_REPO_SLUG_LOWERCASE}}
  FOMO_CACHE_REPOSITORY=${FOMO_CACHE_REPOSITORY:-$FOMO_IMAGE_REPOSITORY}
  FOMO_IMAGE_TAG=${FOMO_IMAGE_TAG:-${CNB_COMMIT:-$(git rev-parse HEAD)}}

  export FOMO_IMAGE_REPOSITORY FOMO_CACHE_REPOSITORY FOMO_IMAGE_TAG
  export FOMO_BAKE_FILES=${FOMO_BAKE_FILES:-docker/docker-bake.hcl:docker/docker-bake.cnb.hcl}
}

login_registry() {
  if [ -n "${CNB_TOKEN:-}" ] && [ -n "${CNB_TOKEN_USER_NAME:-}" ]; then
    printf '%s' "$CNB_TOKEN" | docker login "$CNB_DOCKER_REGISTRY" \
      --username "$CNB_TOKEN_USER_NAME" --password-stdin
  fi
}

build_images() {
  select_images
  [ -s "$images_file" ] || return 0
  validate_web_contract
  configure_registry
  login_registry

  echo "CNB build started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  CI_COMMIT_SHA="$FOMO_IMAGE_TAG" \
    FOMO_PUSH_IMAGES=true \
    FOMO_BUILD_SEQUENTIAL=false \
    sh scripts/ci/build-images.sh "$images_file"
  echo "CNB build finished at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

verify_images() {
  [ -s "$images_file" ] || select_images
  [ -s "$images_file" ] || return 0
  validate_web_contract
  configure_registry
  login_registry

  case "$verify_parallelism" in
    ''|*[!0-9]*|0) echo 'FOMO_VERIFY_PARALLELISM must be a positive integer' >&2; exit 1 ;;
  esac

  echo "CNB verification started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  xargs -P "$verify_parallelism" -n 1 sh "$0" verify-one < "$images_file"
  echo "CNB verification finished at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

verify_one_image() {
  target=${1:?image target is required}
  image="$FOMO_IMAGE_REPOSITORY:$target-$FOMO_IMAGE_TAG"
  echo "Pulling $target image for contract verification"
  docker pull --platform linux/amd64 "$image"
  sh scripts/ci/verify-image.sh "$target" "$image" "$FOMO_IMAGE_TAG" "${FOMO_WEB_API_URL:-}"
}

case "$mode" in
  select) select_images ;;
  build) build_images ;;
  verify) verify_images ;;
  verify-one) verify_one_image "${2:-}" ;;
  all)
    build_images
    verify_images
    ;;
  *) echo 'usage: cnb-build.sh [select|build|verify|verify-one|all]' >&2; exit 1 ;;
esac
