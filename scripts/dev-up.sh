#!/bin/sh

# Build and start a complete local FOMO stack without ever printing the env
# file. Compose only uses the file for interpolation, so provider credentials
# stay confined to LiteLLM instead of being injected into Web/API containers.
set -eu

repo_dir=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
cd "$repo_dir"

env_file=${FOMO_ENV_FILE:-.env.local}
if [ ! -f "$env_file" ]; then
  printf '%s\n' "Missing $env_file. Copy .env.example to .env.local and configure a LiteLLM model route first." >&2
  exit 1
fi

docker compose --env-file "$env_file" config --quiet
docker compose --env-file "$env_file" build sandbox-image api web
docker compose --env-file "$env_file" up --detach --no-build
docker compose --env-file "$env_file" ps
