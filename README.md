# FOMO

FOMO is a web coding agent workbench that runs **Pi, OpenCode, or Codex CLI as a
per-run selectable Coding Agent runtime** inside an OpenSandbox generation
sandbox, with FOMO as the single persistent control plane for verification,
versions, and provenance. GoalGraph is the single planning and execution model.
A real OpenSandbox canary has reached
`succeeded / ready`, and local Chrome/Playwright has verified both the direct
generated-app endpoint and the host-based Preview gateway through interaction
plus reload persistence. These are real local-runtime results, not evidence of
a completed public HTTPS deployment.
Context OS and same-condition A-B validation remain incomplete, so the project
is not yet claimed as security-converged:
Pi uses its official builtin tools with full project permission over a
**mutable Base Snapshot**, and authoritative QA runs only in FOMO's clean
verification sandbox with FOMO-owned direct commands and injected acceptance
tests.

**Current execution model:** GoalGraph, deterministic multi-goal execution,
goal-scoped acceptance, durable verified checkpoints/recovery, and the workbench
Goal panel are the production execution path. A reusable Pi session, automatic
compaction, verified checkpoint
capsules, and bounded repair diagnostics are implemented; a standalone Context
Inspector, semantic reuse registry, and benchmark-driven routing policy are
**not implemented**.

The release-candidate verification results and known limitations are recorded
in [SUBMISSION.md](SUBMISSION.md). Gateway unit/integration coverage, a real
OpenSandbox run, and public DNS/TLS/Tunnel acceptance are distinct evidence
levels; no public HTTPS browser acceptance has completed yet.

## One-command local delivery

Prerequisites:

- Docker Desktop with Linux containers enabled (Apple Silicon is supported).
- A working model route configured for LiteLLM. The repository default is
  `FOMO_RUNTIME_ENABLED_PROFILES=deepseek-flash`, backed by
  `DEEPSEEK_API_KEY`; override the enabled/default profile to match the route
  you have actually verified. GPT profiles use `OPENAI_API_KEY`, Grok uses
  `GROK_API_KEY`, and Kimi/Gemini use `OPENCODE_API_KEY`. Keep real provider
  credentials only in `.env.local`; never commit or print that file. Provider
  credentials are injected only into LiteLLM, never the control plane or a
  generation sandbox.

If a local environment file does not already exist, copy `.env.example` to
`.env.local` and fill in only the model credentials you use. Do not overwrite an
existing `.env.local`.

`FOMO_AGENT_ENABLED_FRAMEWORKS=pi,opencode,codex` controls the public framework
allowlist and `FOMO_AGENT_DEFAULT_FRAMEWORK=pi` selects the initial UI value.
The selected framework, model and thinking level are frozen together when a
run is created; workers never silently fall back to another framework. Codex
CLI is intentionally limited to the GPT-5.5 and GPT-5.6 profiles.

Start the complete stack:

```bash
./scripts/dev-up.sh
```

The script validates Compose configuration, builds the sandbox image before the
application images, waits for PostgreSQL, LiteLLM, and OpenSandbox,
then runs a bounded Direct Pi runtime preflight before starting API, worker, and
Web. The preflight creates a short-lived OpenSandbox, executes a silent probe
inside it against `SANDBOX_LITELLM_BASE_URL`, and proves a bounded streamed
function call through every explicitly enabled alias. It then revokes the
temporary key and destroys the sandbox, so it can incur a very small provider
charge. Optional profiles remain disabled until they are added to the
comma-separated `FOMO_RUNTIME_ENABLED_PROFILES` allowlist; an enabled profile
that fails its canary keeps the worker from starting rather than silently
falling back. On a repeated
launch it first stops prior Compose API, worker, and Web containers so a stale
worker cannot claim work before the canary passes. The script intentionally
invokes Compose with `--env-file .env.local`:
plain `docker compose up` does not automatically load that private file and
therefore may not use your configured model route.

Local endpoints:

- Web: `http://localhost:3000`
- API health: `http://localhost:8000/health`
- Optional Preview gateway health: `http://localhost:8001/_fomo_gateway/healthz`
- LiteLLM: `http://localhost:4000`

Use `docker compose --env-file .env.local logs -f api worker web` for runtime
logs, and `docker compose --env-file .env.local down` to stop the stack without
deleting persistent volumes.

## Coding Agent runtimes

Pi, OpenCode, and Codex CLI share the same GoalGraph, workspace safety audit, clean
Playwright verification, Preview and version publication pipeline. Pi uses the
root-owned RPC bridge. OpenCode runs as a loopback-only server inside the same
generation sandbox through a pinned SDK bridge; it receives only the run-scoped
LiteLLM virtual key and never a provider or LiteLLM master credential. Codex
uses pinned `codex exec --json`/`resume` through a root-owned adapter and the
same run-scoped key; the surrounding OpenSandbox remains the hard isolation
boundary.

### Direct Pi

A run is executed by `WorkerRunner → DirectPiOrchestrator → fomo-pi-ds`:

- **G (generation sandbox):** FOMO seeds a writable `/workspace` from the
  starter base + selected capabilities (Base Snapshot), then one persistent
  `fomo-pi-ds` session implements the active GoalGraph goals during an
  uninterrupted execution. Recovery starts a fresh session from the latest
  verified checkpoint instead of trusting an orphan process. Pi has **full project
  development permission**: official builtin `read/write/edit/bash/grep/find/
  ls`, it may add/move/delete project files and modify package/config/starter
  files, run `pnpm` commands, dev servers, and self-checks. There is **no
  business-file allowlist** and no frozen file plan enforcement; GoalGraph is
  the authoritative delivery contract.
- **Settle audit:** FOMO keeps only real safety invariants — normalized
  in-workspace paths; `.env*` files are rejected outright; `.git/**` (the
  G-internal checkpoint) is excluded; no symlinks/devices or non-regular
  files; only real changed/new files enter the diff (`pnpm-lock.yaml` is
  allowed up to the 512 KiB persistence limit); no business-file count or
  ordinary source-size development quota;
  FOMO-owned roots (`tests/fomo-acceptance/**`, `tests/harness/**` —
  present-and-unchanged files are excluded, any add/modify/delete fails);
  and the system `.gitignore`. There is no full-content secret scanner, only
  `.env*` path rejection plus event redaction. Git commands inside G are
  permitted but carry no release semantics: candidate commits/tags and
  versions are only ever made by FOMO in V.
- **V (verification sandbox):** a clean sandbox seeded from the same Base
  Snapshot receives the complete audited candidate diff (including deletions
  and package/config changes), then FOMO injects the compiled
  `tests/fomo-acceptance/**` Playwright tests. Gates use a fixed PATH composed
  only of root-owned directories and directly execute absolute,
  pnpm-generated `#!/bin/sh` wrappers for `tsc`, `next`, and `playwright`
  from the image runtime cache. Those wrappers are root-owned mode `0555` and
  resolve the trusted system Node; FOMO never resolves runners from the
  candidate's `node_modules/.bin` or model-editable package scripts. Dependency
  setup uses
  `pnpm install --offline --frozen-lockfile --ignore-scripts` (candidate
  lifecycle scripts are blocked). GoalGraph QA scope is selected server-side:
  a non-final Goal uses focused QA, skips `next build`, and runs only that
  Goal's acceptance tests; full QA keeps the build gate and runs every verified
  Goal plus the current Goal's acceptance tests. Full QA is forced for the
  final Goal, project-level configuration changes, files changed again after an
  earlier Goal, legacy checkpoints without `goalChangedPathsByGoal`, and
  verified-graph recovery. After candidate code executed, FOMO-owned
  tests and the harness are re-injected/restored and hash-verified before
  Playwright. Immediately after the initial candidate commit, FOMO freezes
  the V manifest (published files == commit == gate input). Publication then
  requires a final consistency check — the live V's visible source hashes
  must still equal the frozen snapshot — plus a final preview health
  recheck; the version persists only the frozen manifest with an explicit
  tag pointing at the frozen commit, and `preview.verified` fires only after
  the consistency check, health recheck, and version creation all succeed
  (a dead dev server fails closed without a version). This is hardening, not a host-level anti-tamper boundary: an
  adversarial same-user race that tampers with the writable
  acceptance/harness files and restores them is not solved (external QA
  runner / read-only mounts remain the public-deployment release blocker).
  `preview.available` is emitted (unverified) as soon as the dev server is
  healthy. Preview semantics are precise: entering repair destroys the
  current V, clears the URL, and emits `preview.expired` (no preview during
  repair), and infrastructure failures clear it. It is never upgraded without
  full gate evidence. A fully verified OpenSandbox Preview is renewed
  to the bounded seven-day retention window before publication. When
  `PUBLIC_PREVIEW_BASE_URL` is configured, publish stores
  `<base-url>/<sandbox-id>/`; wildcard `PUBLIC_PREVIEW_BASE_DOMAIN` remains
  compatible and stores `https://<sandbox-id>.<domain>/`. Internal health checks still use the direct
  endpoint, avoiding a circular dependency on the not-yet-published gateway
  authorization. Pi self-checks are never release evidence.

Dependency note: verification installs **offline from FOMO's prefetched
package store**. A dependency that is not in the store will fail the install
gate honestly; arbitrary `pnpm add` is not yet a supported release path.

The bridge (`infra/opensandbox/fomo-pi-rpc-bridge.mjs`) is a transport/
observation/cancellation layer only: JSONL protocol, event and usage
observation (including A/B telemetry such as first-tool and
first-edit/write-tool timing — `firstEditOrWriteToolElapsedMs`; bash-side
writes are not counted because the bridge does not interpret tool
semantics), cancellation, heartbeats, redaction, fail-closed parsing, and
session reuse. Provider context/output limits, spend limits, sandbox lifetime,
lease loss, cancellation, and transport failure remain real boundaries; FOMO
does not impose a cumulative run-token ceiling. The bridge does not proxy or
rewrite Pi tool semantics. The per-run virtual key is blocked at run end as a
best-effort step, with the key TTL as the fallback.

## Optional desktop proxy

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
network access. The control plane passes only those three variables as the
`env` field of `Sandbox.create`; it never inherits generic container, model,
provider, or OpenSandbox credentials. Proxy URLs with user-info are rejected.

The `opensandbox` lifecycle endpoint is bound only to `127.0.0.1` on port
`8080`. Its metadata lives in the `opensandbox-metadata` Docker volume. API and
worker call `http://opensandbox:8080` over the Compose network and never mount
`/var/run/docker.sock`.

`docker compose build sandbox-image` produces the fixed
`fomo-sandbox-node:2026-08-08` image used by `OPENSANDBOX_IMAGE`. The image
uses the non-root `node` user with a writable `/workspace`, Node 22, pnpm
pinned through Corepack, Git, Playwright Chromium, and the read-only
`fomo-next-radix-v2` starter assets (copied into the writable workspace).
Generated product code is not restricted to fixed model-owned roots: the whole
project, including package/config/starter files, is mutable. The v2 base is
deliberately generic (Next/TypeScript/Tailwind, vendored shadcn/Radix
primitives, responsive shell slots, reusable states); its harness smoke lives
under `tests/harness/**` and is FOMO-owned.

The API and worker share a Python 3.11 image. Provider keys remain only in
LiteLLM; a run-scoped opaque virtual key is the only credential inside the
generation sandbox. The Next.js image uses its existing standalone output and
bakes only the browser-safe `NEXT_PUBLIC_API_URL` into the client bundle.

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
gVisor and an authenticated egress policy.

**Egress isolation is not implemented.** The local OpenSandbox config has
no authenticated `dns+nft` egress sidecar, the policy API is unauthenticated
(workloads could read/rewrite it), and host-only rules would still allow
other ports of the host gateway — so FOMO deliberately ships **no** network
policy switch that looks fail-closed but is bypassable. Local trusted
development is fine; **public untrusted deployment is a release blocker**
until an authenticated dns+nft/credential-proxy egress path is implemented
and verified.

FOMO has no host-process sandbox fallback. If OpenSandbox is unavailable, the
run fails closed instead of executing generated code on the control-plane host.

## Controlled public Preview gateway

The optional `public-preview` Compose profile starts `fomo-preview-gateway` on
loopback port `8001`. It supports the existing wildcard-host mode, a same-site
`/preview/<sandbox-id>/` path routed through Web's official Next.js external
rewrite, or that path on a fixed dedicated cross-site tunnel origin. Production
derives `WEB_ORIGIN/preview` when neither public Preview setting is explicit, so
no new DNS entry is required. For every request the gateway:

- accepts one canonical sandbox UUID host label or path segment only;
- requires that persistence still identifies it as the live, uncleaned
  verification sandbox of a succeeded run;
- resolves the random OpenSandbox host port server-side, so the browser never
  receives a `localhost:<random-port>` URL and root-relative `/_next/*` assets
  keep working;
- strips FOMO cookies, authorization, forwarding headers, the OpenSandbox key,
  and generated-app `Set-Cookie` responses;
- applies `no-store`; same-site path HTML receives an opaque CSP sandbox without
  `allow-same-origin` or forms. A configured URL on a different registrable site
  permits same-origin hydration/storage and forms, but allows only `WEB_ORIGIN`
  to frame it, disables workers, and strips `Service-Worker-Allowed` and
  `Clear-Site-Data`. Resource, connection, and form CSP paths are defense in
  depth only; redirect handling means URL paths are not an isolation boundary;
- records `preview.expired` and removes the durable URL only after an
  authoritative OpenSandbox 404/410; transient provider failures return 502.

The Compose service has its own minimal environment instead of inheriting the
control-plane environment. It receives only the application database URL,
OpenSandbox lifecycle URL/key, `APP_ENV`, `WEB_ORIGIN`, public Preview route,
upstream host override, and listen port. In particular, it has no LiteLLM master key, provider/model
credential, or unrelated worker configuration.

Start the local profile with
`docker compose --profile public-preview up -d preview-gateway` after building
the control-plane image. This gateway solves routing, not hostile-code
isolation: the controlled evaluator deployment must still use authenticated
ingress, firewall the lifecycle/database/model ports and enforce cost/rate
limits. It must not be opened as an anonymous public generator under the local
Docker trust model below.

[`deploy/cloudflared/config.example.yml`](deploy/cloudflared/config.example.yml)
contains the minimal named-Tunnel routing shape. It intentionally separates the
workbench (`app.example.com`) from generated code
(`*.fomo-previews.example.net`) at the eTLD+1 boundary. `/v1/*` and `/health`
go to API, the remaining app hostname goes to Web, and the wildcard Preview
hostname goes to port 8001. `cloudflared` is the only required reverse proxy;
do not add an `httpHostHeader` override to the Preview rule because the UUID
Host is part of gateway authorization.

Before building Web, set `NEXT_PUBLIC_API_URL=https://app.example.com`. With
`APP_ENV=production` and `WEB_ORIGIN=https://app.example.com`, path mode needs
no additional setting. To use the stronger wildcard mode instead, set
`PUBLIC_PREVIEW_BASE_DOMAIN=fomo-previews.example.net`. The Preview name must
be a delegated zone of its own or have an explicit certificate covering
`*.fomo-previews.example.net`; parent-zone Universal SSL does not normally
cover that deeper hostname. Configure the entire Preview hostname to bypass
Cloudflare cache so an expired/replaced sandbox is never served from edge
cache. Use a named Tunnel plus Access policy for the workbench; a random Quick
Tunnel is not a release URL. Validate the completed configuration with
`cloudflared tunnel ingress validate` before starting it.

As a deployment-light alternative, a fixed authenticated tunnel can route a
dedicated HTTPS origin, such as an sslip.io hostname, to the
loopback Preview Gateway and set that value as `PUBLIC_PREVIEW_BASE_URL`. The
registrable site must differ from `WEB_ORIGIN`; otherwise the gateway deliberately
accepts only the exact `WEB_ORIGIN` for opaque path mode and rejects a different
same-site origin. Non-loopback Preview URLs must use HTTPS. Cross-site mode
restores forms, Next hydration, and
localStorage without exposing the origin port, but every `/preview/<uuid>/`
under that hostname shares one browser origin, script-visible cookies, and
localStorage. Workers are disabled, but this remains a trusted,
single-tenant demo mode rather than isolation between Preview IDs. Do not use it
for mutually untrusted previews; wildcard mode gives each Preview its own origin.

The public surface is HTTPS at the Cloudflare edge only. With Tunnel, the
origin needs no inbound Internet port; otherwise its firewall may expose only
the TLS ingress. Never expose OpenSandbox `8080`, LiteLLM `4000`, Docker's
random generated-Preview ports (including `40000-60000`), PostgreSQL `5432`,
or other internal service ports. Recreate older Compose containers after
changing loopback port bindings and verify the live bindings, not just the YAML.

The generated-app gateway supports ordinary HTTP request/response Next.js
pages only. Generated applications that require WebSocket, SSE, or arbitrary
streaming transport are outside this demo contract. Publication renews the
verified sandbox once for at most seven days; keep Docker, OpenSandbox, the
gateway, and `cloudflared` running, check the recorded expiry before submission,
and renew or rerun before it expires if the evaluator window is longer.

Local gateway checks are not public-deployment evidence. Public delivery is
complete only after an external-network browser verifies TLS and DNS, account
login, one real generation with SSE updates, the final HTTPS Preview URL,
`/_next/*` resources, interaction, and persistence after reload.
