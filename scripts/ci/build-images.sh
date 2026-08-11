#!/bin/sh
set -eu

images_file=${1:-.ci/images}
tag=${CI_COMMIT_SHA:-$(git rev-parse HEAD)}
image_repository=${FOMO_IMAGE_REPOSITORY:-}
bake_files=${FOMO_BAKE_FILES:-${FOMO_BAKE_FILE:-docker/docker-bake.hcl}}
push_images=${FOMO_PUSH_IMAGES:-false}
sequential=${FOMO_BUILD_SEQUENTIAL:-true}

image_repository=${image_repository%/}

[ -S /var/run/docker.sock ] || {
  echo 'Docker socket is unavailable' >&2
  exit 1
}

case "$push_images" in
  true|false) ;;
  *) echo 'FOMO_PUSH_IMAGES must be true or false' >&2; exit 1 ;;
esac
case "$sequential" in
  true|false) ;;
  *) echo 'FOMO_BUILD_SEQUENTIAL must be true or false' >&2; exit 1 ;;
esac

if [ ! -s "$images_file" ]; then
  echo 'No application image needs to be built'
  exit 0
fi

export DOCKER_BUILDKIT=1
export BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}"
export FOMO_IMAGE_REPOSITORY="$image_repository"
export FOMO_IMAGE_TAG="$tag"
export FOMO_WEB_API_URL="${FOMO_WEB_API_URL:-http://localhost:8000}"
export FOMO_WEB_PREVIEW_GATEWAY_INTERNAL_URL="${FOMO_WEB_PREVIEW_GATEWAY_INTERNAL_URL:-http://preview-gateway:8001}"

images=$(tr '\n' ' ' < "$images_file" | sed 's/ *$//')
for image in $images; do
  case "$image" in
    control-plane|sandbox|web) ;;
    *) echo "Unsupported image target: $image" >&2; exit 1 ;;
  esac
done

echo "Building targets for revision $tag: $images"

bake_file_args=""
old_ifs=$IFS
IFS=:
for bake_file in $bake_files; do
  [ -n "$bake_file" ] || continue
  [ -f "$bake_file" ] || {
    echo "Bake file not found: $bake_file" >&2
    exit 1
  }
  bake_file_args="$bake_file_args --file $bake_file"
done
IFS=$old_ifs
[ -n "$bake_file_args" ] || {
  echo 'No Bake file configured' >&2
  exit 1
}

run_bake() {
  # Bake file paths and targets are repository-controlled and validated above.
  # shellcheck disable=SC2086
  if [ "$push_images" = true ]; then
    docker buildx bake $bake_file_args --push "$@"
  else
    docker buildx bake $bake_file_args --load "$@"
  fi
}

if [ "$sequential" = true ]; then
  for image in $images; do
    echo "Building $image sequentially"
    run_bake "$image"
  done
else
  # Targets are validated above and intentionally expanded for parallel Bake.
  # shellcheck disable=SC2086
  run_bake $images
fi

for target in $images; do
  if [ -n "$image_repository" ]; then
    image="$image_repository:$target-$tag"
  else
    image="fomo-local/$target:$tag"
  fi

  if [ "$push_images" = true ]; then
    docker buildx imagetools inspect "$image" >/dev/null
  else
    docker image inspect "$image" >/dev/null
  fi
done
