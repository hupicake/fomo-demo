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

### Optional official DeepSeek Flash route

FOMO registers the official LiteLLM alias `deepseek-flash` for
`deepseek/deepseek-v4-flash`; no DeepSeek Pro model is registered. Put a real
`DEEPSEEK_API_KEY` only in `.env.local`, then set all four roles to this alias
when you want to use it:

```bash
MODEL_PM=deepseek-flash
MODEL_ARCHITECT=deepseek-flash
MODEL_ENGINEER=deepseek-flash
MODEL_REVIEWER=deepseek-flash
```

### Engineer file-size policy

Engineer source files use a 12,000-character split target and a 20,000-character
hard rejection limit, configurable with `ENGINEER_TARGET_FILE_CHARACTERS` and
`ENGINEER_MAX_FILE_CHARACTERS`. Both must be positive, target must not exceed
hard, and hard must not exceed 24,000. Content over the target through the hard
limit succeeds and emits one aggregate, source-free `file_batch_over_target`
activity; only content over the hard limit is rejected.

### Reviewer repair scope

For a failed compiler gate, FOMO keeps compiler-reported raw paths separate
from deterministic derived dependency paths. It may add only direct planned,
non-delete, model-owned TypeScript providers or consumers resolved from local
static imports/exports (relative paths or `@/`, including `index`, `.ts`, and
`.tsx`) and explicitly declared direct TechnicalSpec relationships: feature
surface composition/modules when both symbols bind public APIs, plus state
aggregation links to each persistent-domain actions store and its persistence
adapter. Dynamic, package, unknown,
ambiguous, unplanned, or protected targets never expand the scope. The shared
raw-plus-derived scope constrains both Reviewer and repair planning; it must
contain at most eight files or fail closed. A build or smoke failure with no
compiler file evidence remains `evidence_missing` rather than guessing a file.

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
and the read-only `fomo-next-radix-v2` Golden Starter assets. Every workspace
copies a digest-pinned base plus only the Architect-selected approved
capabilities before its first Git commit. The initial provenance records the
base, selected capability IDs and versions, composite digest, and per-file
hashes; an unknown, duplicate, conflicting, or tampered capability fails
closed. Do not add OpenSandbox `execd` to it: the OpenSandbox server injects
that runtime component when it creates a sandbox.

The v2 base is deliberately generic: it supplies the Next/TypeScript/Tailwind
foundation, vendored shadcn/Radix primitives, Geist typography, responsive app
shell slots, and reusable loading/empty/error/toast/confirmation/validation
states. Its protected Playwright harness includes one domain-neutral smoke spec
so the bare base and every capability selection can pass the fixed smoke entry;
generated acceptance tests remain limited to `tests/generated/**`. The protected
root page delegates only to `app/(generated)/composition.tsx`, whose required
named export is `GeneratedComposition`. It does not prescribe an entity, fields,
business rules, navigation labels, storage key, authentication, payment, API,
or information architecture.
The only currently approved optional capabilities are `crud` and
`local-persistence`; they are fixed source overlays, not model-installed
packages. The catalog describes `crud` as generic client collection
state/actions/render slots and `local-persistence` as an SSR-safe typed,
versioned localStorage migration adapter. Generated product code is restricted to `app/(generated)/**`,
`components/features/**`, `lib/domain/**`, and `tests/generated/**`.
The official Next.js, shadcn/ui, and v0 references inform these starter
patterns only; FOMO does not import a v0 SDK, v0 runtime, v0 API, or v0 key.

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
