# FOMO

FOMO is a four-role coding agent workbench. It persists the requirement-to-
evidence trail and uses MetaGPT for role collaboration, LiteLLM for model
routing, and OpenSandbox for generated-code execution and preview.

## One-command local delivery

Prerequisites:

- Docker Desktop with Linux containers enabled (Apple Silicon is supported).
- A model route configured for LiteLLM. Keep real provider credentials only in
  `.env.local`; never commit or print that file. Node and Python are bundled in
  the application images, so they are not required on the host for this path.

If a local environment file does not already exist, copy `.env.example` to
`.env.local` and fill in only the model credentials you use. Do not overwrite an
existing `.env.local`.

Start the complete stack:

```bash
./scripts/dev-up.sh
```

The script validates Compose configuration, builds the sandbox image before the
application images, then starts PostgreSQL, Redis, MinIO, LiteLLM,
OpenSandbox, API, worker, and Web. The first run is slower because the worker
image installs the SHA-pinned MetaGPT extra. It intentionally invokes Compose
with `--env-file .env.local`: plain `docker compose up` does not automatically
load that private file and therefore may not use your configured model route.

Local endpoints:

- Web: `http://localhost:3000`
- API health: `http://localhost:8000/health`
- LiteLLM: `http://localhost:4000`
- MinIO console: `http://localhost:9001`

Use `docker compose --env-file .env.local logs -f api worker web` for runtime
logs, and `docker compose --env-file .env.local down` to stop the stack without
deleting persistent volumes.

### Engineer file-size policy

Engineer source files use a 12,000-character split target and a 20,000-character
hard rejection limit, configurable with `ENGINEER_TARGET_FILE_CHARACTERS` and
`ENGINEER_MAX_FILE_CHARACTERS`. Both must be positive, target must not exceed
hard, and hard must not exceed 24,000. Content over the target through the hard
limit succeeds and emits one aggregate, source-free `file_batch_over_target`
activity; only content over the hard limit is rejected.

### Optional desktop proxy

Proxying is disabled by default. If an opt-in local proxy listens at
`http://127.0.0.1:7890` on macOS, set `DOCKER_HTTP_PROXY` and
`DOCKER_HTTPS_PROXY` in `.env.local` to `http://host.docker.internal:7890`.
Compose forwards those values only to LiteLLM and Docker build steps; its
default `NO_PROXY` list bypasses FOMO's internal services. Do not put proxy
credentials in these variables, because container environments and build
processes can inspect them.

Docker image pulls themselves are performed by Docker Desktop. If Docker cannot
fetch a base image, configure its daemon proxy separately; `DOCKER_*` only
controls the containers and build steps described above.

Generated OpenSandbox projects have a separate, explicit opt-in policy:
`SANDBOX_HTTP_PROXY`, `SANDBOX_HTTPS_PROXY`, and `SANDBOX_NO_PROXY`. Set these
to the same host-reachable proxy endpoint only when generated projects need
network access for commands such as `pnpm install`. The control plane passes
only those three variables as the `env` field of `Sandbox.create`; it never
inherits generic container, model, provider, or OpenSandbox credentials. Proxy
URLs with user-info are rejected, so authenticated proxy credentials must not
enter generated sandboxes.

The `opensandbox` lifecycle endpoint is bound only to `127.0.0.1` on port
`8080`. Its metadata lives in the `opensandbox-metadata` Docker volume. API and
worker call `http://opensandbox:8080` over the Compose network and never mount
`/var/run/docker.sock`.

`docker compose build sandbox-image` produces the fixed
`fomo-sandbox-node:2026-08-08` image used by `OPENSANDBOX_IMAGE`. The
`sandbox-image` Compose service is in the `build` profile and cannot start in a
normal `docker compose up`; it exists only as a reproducible image build target.
The image uses the non-root `node` user with a writable `/workspace`, Node 22,
pnpm pinned through Corepack, Git, Playwright Chromium, native Node build tools,
and the read-only `fomo-next-radix-v1` source seed. Every workspace copies and
verifies that seed before its first Git commit. Do not add OpenSandbox `execd`
to it: the OpenSandbox server injects that runtime component when it creates a
sandbox.

The API and worker share a Python 3.11 image. Its build installs the locked
`metagpt` extra because `AGENT_FRAMEWORK=metagpt` is the default; provider keys
remain only in LiteLLM. The Next.js image uses its existing standalone output
and bakes only the browser-safe `NEXT_PUBLIC_API_URL` into the client bundle.

## Local sandbox trust boundary

OpenSandbox is the only service in `compose.yaml` with access to the Docker
socket. It creates sandbox containers through its lifecycle API; the FOMO
control plane and worker interact only through that API. A preview always uses
the generated app's fixed port `8080`; the `execd` control port `44772` is never
returned to a browser.

On macOS Docker Desktop this runs the Docker `runc` runtime, which is suitable
for trusted local development and acceptance testing only. It is not a strong
isolation boundary for hostile code and must not be exposed publicly. A public
deployment needs a dedicated Linux runner plus a stronger runtime such as
gVisor, as documented in `DESIGN.md`.

FOMO does not silently fall back to a host-process sandbox if OpenSandbox is
unavailable. `SANDBOX_PROVIDER=process` is an explicit, separately guarded
trusted-development option only; a failed OpenSandbox run must surface as a
failed run rather than a false preview success.

E2B remains an optional future cloud-provider adapter. Local startup, testing,
and acceptance require no E2B account or key.
