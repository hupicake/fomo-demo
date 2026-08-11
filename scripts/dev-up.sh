#!/bin/sh

# Build and start a complete local FOMO stack without ever printing the env
# file. Compose only uses the file for interpolation, so provider credentials
# stay confined to LiteLLM instead of being injected into Web/API containers.
set -eu

repo_dir=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
cd "$repo_dir"

env_file=${FOMO_ENV_FILE:-.env.local}
wait_timeout=${FOMO_WAIT_TIMEOUT_SECONDS:-300}
if [ ! -f "$env_file" ]; then
  printf '%s\n' "Missing $env_file. Copy .env.example to .env.local and configure a LiteLLM model route first." >&2
  exit 1
fi

docker compose --env-file "$env_file" config --quiet
# Fail closed on repeated launches: an older Compose worker must not claim work
# while the replacement stack is still being built or preflighted.
docker compose --env-file "$env_file" stop api worker web
docker compose --env-file "$env_file" build sandbox-image api web
docker compose --env-file "$env_file" up --detach --no-build --wait \
  --wait-timeout "$wait_timeout" \
  postgres litellm opensandbox
# Run the paid, bounded OpenSandbox -> LiteLLM canary only after infrastructure
# is healthy and before the user-facing app is declared ready. The command reads
# credentials from the service environment and never places them in argv/output.
docker compose --env-file "$env_file" run --rm --no-deps worker fomo-runtime-preflight
docker compose --env-file "$env_file" up --detach --no-build --wait \
  --wait-timeout "$wait_timeout" \
  api worker web
docker compose --env-file "$env_file" ps
