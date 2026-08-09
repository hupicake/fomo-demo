# FOMO

FOMO is a web coding agent workbench that drives **Pi (the coding agent) as a
replaceable execution kernel** inside an OpenSandbox generation sandbox, with
FOMO as the single persistent control plane for verification, versions, and
provenance. The current implementation contains the **P0 native Pi baseline**
and the **P1-A GoalGraph vertical slice**. A real OpenSandbox canary has reached
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

**Transition state (honest):** GoalGraph, deterministic multi-goal execution,
goal-scoped acceptance, durable verified checkpoints/recovery, and the workbench
Goal panel are implemented behind `DIRECT_PI_GOAL_GRAPH_ENABLED` (enabled by
default). Context OS (Manifest/Capsule/Inspector), Verified Reuse Registry, and
Policy/Micro-tuning are **not implemented**. See `DESIGN.md` for the staged
plan. The legacy native
four-role SOP still exists as a **non-default writable compatibility path**
(explicit `AGENT_FRAMEWORK=native`) and is not the production chain
(`AGENT_FRAMEWORK=direct_pi` is the default).

Current regression evidence is 385 backend tests, 134 Web Vitest tests, 18 Pi
bridge tests, and 2/2 local Web Playwright tests covering the workbench smoke
and account/session isolation, with Web typecheck/build and Ruff also green.
Gateway unit/integration coverage is part of the backend suite; the real
OpenSandbox and Chrome checks are separate runtime evidence. No public
DNS/TLS/Tunnel browser acceptance has completed yet.

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
OpenSandbox, API, worker, and Web. It intentionally invokes Compose with
`--env-file .env.local`: plain `docker compose up` does not automatically load
that private file and therefore may not use your configured model route.

Local endpoints:

- Web: `http://localhost:3000`
- API health: `http://localhost:8000/health`
- Optional Preview gateway health: `http://localhost:8001/_fomo_gateway/healthz`
- LiteLLM: `http://localhost:4000`
- MinIO console: `http://localhost:9001`

Use `docker compose --env-file .env.local logs -f api worker web` for runtime
logs, and `docker compose --env-file .env.local down` to stop the stack without
deleting persistent volumes.

## Direct Pi runtime (production path)

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
  authoritative when enabled, while the legacy BuildPlan is advisory/read-only
  compatibility data.
- **Settle audit:** FOMO keeps only real safety invariants — normalized
  in-workspace paths; `.env*` files are rejected outright; `.git/**` (the
  G-internal checkpoint) is excluded; no symlinks/devices or non-regular
  files; only real changed/new files enter the diff (`pnpm-lock.yaml` is
  allowed up to the 512 KiB persistence limit); bounded changed-file counts;
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
  repair); only when repair rounds are exhausted with a healthy preview
  already present is it kept best-effort (NEEDS_ATTENTION does not guarantee
  a preview), and infrastructure failures clear it. It is never upgraded
  without full gate evidence. A fully verified OpenSandbox Preview is renewed
  to the bounded seven-day retention window before publication. When
  `PUBLIC_PREVIEW_BASE_DOMAIN` is configured, the final atomic publish stores
  `https://<sandbox-id>.<domain>/`; internal health checks still use the direct
  endpoint, avoiding a circular dependency on the not-yet-published gateway
  authorization. Pi self-checks are never release evidence.

Dependency note: verification installs **offline from FOMO's prefetched
package store**. A dependency that is not in the store will fail the install
gate honestly; arbitrary `pnpm add` is not yet a supported release path.

The bridge (`infra/opensandbox/fomo-pi-rpc-bridge.mjs`) is a transport/
observation/cancellation/budget layer only: JSONL protocol, event and usage
observation (including A/B telemetry such as first-tool and
first-edit/write-tool timing — `firstEditOrWriteToolElapsedMs`; bash-side
writes are not counted because the bridge does not interpret tool
semantics), cancellation, wall-clock/silence/timeout budgets, redaction,
fail-closed parsing, and session reuse. It does not proxy or rewrite Pi tool
semantics. The per-run virtual key is blocked at run end as a best-effort
step, with the key TTL as the fallback.

## Legacy native SOP path (non-default, writable)

MetaGPT has been retired; `AGENT_FRAMEWORK` accepts only `direct_pi` and
`native`.

The old four-role chain (Product Manager → Architect → Engineer → Reviewer)
remains available while historical runs and focused compatibility tests are
retired. It is **not** the production path and is not read-only: explicitly
selecting `AGENT_FRAMEWORK=native` makes it a fully writable
compatibility path. Legacy role aliases (`MODEL_PM`, `MODEL_ARCHITECT`,
`MODEL_ENGINEER`, `MODEL_REVIEWER`), the Engineer file-size/batch policy, and
the Reviewer repair scope rules apply only to that legacy path.

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
gVisor, as documented in `DESIGN.md`.

**Egress isolation is not implemented.** The local OpenSandbox config has
no authenticated `dns+nft` egress sidecar, the policy API is unauthenticated
(workloads could read/rewrite it), and host-only rules would still allow
other ports of the host gateway — so FOMO deliberately ships **no** network
policy switch that looks fail-closed but is bypassable. Local trusted
development is fine; **public untrusted deployment is a release blocker**
until an authenticated dns+nft/credential-proxy egress path is implemented
and verified.

FOMO does not silently fall back to a host-process sandbox if OpenSandbox is
unavailable. `SANDBOX_PROVIDER=process` is an explicit, separately guarded
trusted-development option only; a failed OpenSandbox run must surface as a
failed run rather than a false preview success.

## Controlled public Preview gateway

The optional `public-preview` Compose profile starts `fomo-preview-gateway` on
loopback port `8001`. A wildcard DNS/TLS ingress routes
`*.PUBLIC_PREVIEW_BASE_DOMAIN` to it. For every request the gateway:

- accepts one canonical sandbox UUID subdomain only;
- requires that persistence still identifies it as the live, uncleaned
  verification sandbox of a succeeded run;
- resolves the random OpenSandbox host port server-side, so the browser never
  receives a `localhost:<random-port>` URL and root-relative `/_next/*` assets
  keep working;
- strips FOMO cookies, authorization, forwarding headers, the OpenSandbox key,
  and generated-app `Set-Cookie` responses;
- records `preview.expired` and removes the durable URL only after an
  authoritative OpenSandbox 404/410; transient provider failures return 502.

The Compose service has its own minimal environment instead of inheriting the
control-plane environment. It receives only the application database URL,
OpenSandbox lifecycle URL/key, public Preview domain, upstream host override,
and listen port. In particular, it has no LiteLLM master key, provider/model
credential, Redis URL, MinIO endpoint, or AWS credential.

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

Before building Web, set `NEXT_PUBLIC_API_URL=https://app.example.com`. At
runtime set `APP_ENV=production`, `WEB_ORIGIN=https://app.example.com`, and
`PUBLIC_PREVIEW_BASE_DOMAIN=fomo-previews.example.net`. The Preview name must
be a delegated zone of its own or have an explicit certificate covering
`*.fomo-previews.example.net`; parent-zone Universal SSL does not normally
cover that deeper hostname. Configure the entire Preview hostname to bypass
Cloudflare cache so an expired/replaced sandbox is never served from edge
cache. Use a named Tunnel plus Access policy for the workbench; a random Quick
Tunnel is not a release URL. Validate the completed configuration with
`cloudflared tunnel ingress validate` before starting it.

The public surface is HTTPS at the Cloudflare edge only. With Tunnel, the
origin needs no inbound Internet port; otherwise its firewall may expose only
the TLS ingress. Never expose OpenSandbox `8080`, LiteLLM `4000`, Docker's
random generated-Preview ports (including `40000-60000`), PostgreSQL `5432`,
Redis `6379`, or MinIO `9000-9001`. Recreate older Compose containers after
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
