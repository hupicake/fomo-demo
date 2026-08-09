# FOMO Control Plane

FastAPI control plane plus an independent durable worker for FOMO's coding
agent runtime. The production path is **Direct Pi**
(`AGENT_FRAMEWORK=direct_pi`): `WorkerRunner → DirectPiOrchestrator →
fomo-pi-ds`, where Pi runs inside an OpenSandbox generation sandbox with its
official builtin tools and full project permission. The runtime now contains
the **P0 native Pi baseline** and the **P1-A GoalGraph vertical slice**.

**Status (honest): P0 and P1-A code are landed.** The latest concentrated code
regression is green: 385 backend tests, 134 Web Vitest tests, 18 Pi bridge
tests, 2/2 local Web Playwright tests, Web typecheck/build, and Ruff. The real
local fixed-runner canary has passed. The ten-run lifecycle matrix is unfinished,
must not be counted as passed, and is not a release condition for the current
take-home Demo. Same-condition A/B validation, Context OS, and public HTTPS
acceptance remain incomplete, so nothing here is claimed as
environment-accepted or security-converged. GoalGraph, goal-scoped acceptance,
durable checkpoints, and recovery are implemented; Verified Reuse remains
planned (`DESIGN.md`) and is **not implemented**.

The Preview Gateway's unit/integration tests are included in the 385-test
backend suite. Real local OpenSandbox and Chrome/Gateway checks are separate
runtime evidence. Neither level proves public DNS, TLS, Tunnel routing, or an
external-network browser flow.

The database owns sessions, projects, runs, durable SSE events, structured
artifacts, trace links, evidence, versions, and text file snapshots. The API
only accepts commands and serves state; generated code and package commands
run in the worker through a `SandboxProvider`.

## Runtime

- Python is pinned to **3.11** (`>=3.11,<3.12`).
- Direct Pi model egress goes through LiteLLM with a per-run opaque virtual
  key (`LiteLLMRunKeyClient`); only `fomo-pi-flash` (planning) and
  `fomo-pi-build` (building/repairing) aliases are allowed, with explicit
  thinking levels and **no silent fallback**. The bridge's model selection is
  fail-closed. Provider credentials stay in LiteLLM and never enter a sandbox.
- An uninterrupted run reuses one `fomo-pi-ds` session across planning,
  multiple GoalGraph goals, and repair turns. Recovery never trusts an orphan
  process: it creates a fresh session from the latest verified checkpoint and
  persisted usage balance. GoalGraph is authoritative when enabled; the legacy
  BuildPlan is **advisory/read-only compatibility data**. BUILDING uses Pi's native
  `read/write/edit/bash` tools and **no business-file write allowlist**. The
  Base Snapshot (starter base + capabilities + prior verified state) is
  mutable: package.json, lockfiles, config, routes, app shell, components, and
  ordinary tests may all be added, moved, modified, or deleted.
- The settle audit (`direct_pi/workspace.py`) enforces only real safety
  invariants: normalized in-workspace paths; `.env*` files are rejected
  outright; `.git/**` (the G-internal checkpoint) is excluded; no
  symlinks/devices or non-regular files; only real changed/new files enter
  the candidate diff (`pnpm-lock.yaml` allowed up to the 512 KiB persistence
  limit); bounded changed-file counts; FOMO-owned roots
  (`tests/fomo-acceptance/**`, `tests/harness/**` — present-and-unchanged
  files are excluded, any add/modify/delete fails) and the system
  `.gitignore`. There is no full-content secret scanner (only `.env*` path
  rejection plus event redaction). Base deletions persist across runs: the
  version manifest is the complete candidate truth, and starter files absent
  from it are deleted on seed.
- Deterministic QA runs only in a clean verification sandbox V seeded from the
  same Base Snapshot, with the complete audited diff applied and
  `tests/fomo-acceptance/**` injected by FOMO. Immediately after the initial
  candidate commit, FOMO freezes the V manifest (published files == commit
  == gate input; bound by a HEAD + clean-worktree check before candidate
  processes start). Gates use a fixed PATH composed only of root-owned
  directories and directly execute absolute, pnpm-generated `#!/bin/sh`
  wrappers for `tsc`, `next`, and `playwright` from the image runtime cache.
  Those wrappers are root-owned mode `0555` and resolve the trusted system
  Node; FOMO never resolves runners from the candidate's `node_modules/.bin`
  or model-editable package scripts. Dependency setup uses `pnpm install
  --offline --frozen-lockfile --ignore-scripts` (candidate lifecycle scripts
  are blocked). GoalGraph QA scope is server-selected: a non-final Goal uses
  focused QA, skips `next build`, and runs only the current Goal's acceptance
  tests. Full QA keeps the build gate and runs all verified Goals plus the
  current Goal's acceptance tests; it is forced for the final Goal,
  project-level configuration changes, files changed again after an earlier
  Goal, legacy checkpoints without `goalChangedPathsByGoal`, and verified-graph
  recovery. After
  candidate code executed, FOMO-owned acceptance tests and the harness are
  re-injected/restored and hash-verified before Playwright. Publication
  requires a final consistency check (live V source hashes must equal the
  frozen snapshot) plus a final preview health recheck; the version persists
  only the frozen manifest with an explicit tag at the frozen commit, and
  `preview.verified` fires only after the consistency check, health recheck,
  and version creation all succeed (a dead dev server fails closed without a
  version). This is hardening, not a host-level anti-tamper
  boundary: a same-user adversarial race that tampers with the writable
  acceptance/harness files and restores them is not solved (external QA
  runner / read-only mounts remain the public-deployment release blocker).
  Dependencies are limited to FOMO's prefetched offline store; a package not
  in the store fails the install gate honestly and goes to repair (an
  ordinary non-zero install is a repairable source/package problem; only
  timeouts, missing runners, or restore failures are infrastructure).
- A/B telemetry is emitted by production events (bridge `pi.tool.*`/
  `pi.completed` telemetry, `preview.available/verified` elapsedSeconds);
  there is no benchmark runner. A/B execution is part of the upcoming
  central verification matrix and has not been executed yet.
- `SandboxProvider` defaults to `opensandbox` (OpenSandbox Server v0.2.2, SDK
  v0.1.15): arm64 workspace from `fomo-sandbox-node:2026-08-08`, execd command
  streaming, file reads/writes, pause/kill, previews via `get_endpoint(8080)`.
  A fully verified Preview is renewed to the bounded seven-day retention
  window before version publication; renewal failure fails closed without a
  version or `preview.verified` event.
  Port `44772` is execd, never a preview. **Egress isolation is not
  implemented**: the local OpenSandbox config has no authenticated `dns+nft`
  sidecar, the policy API is unauthenticated, and host-only rules would still
  allow other host-gateway ports — so no network-policy switch is shipped.
  Local trusted development is fine; public untrusted deployment is a release
  blocker until an authenticated egress path is implemented and verified.
- Generated-code sandboxes never inherit worker proxy variables. Only
  `SANDBOX_HTTP_PROXY` / `SANDBOX_HTTPS_PROXY` / `SANDBOX_NO_PROXY` cross the
  boundary when set, and only the run-scoped virtual key is injected as a
  credential.
- `ProcessSandboxProvider` is **only** for trusted local development/CI and
  requires `ALLOW_UNSAFE_PROCESS_SANDBOX=true`; it is not a fallback for
  OpenSandbox and is not safe for public user input.

## Legacy native SOP path

The MetaGPT integration has been retired; `AGENT_FRAMEWORK` accepts only
`direct_pi` and `native`.

The four-role chain (Product Manager → Architect → Engineer → Reviewer) is
retained while historical runs and focused compatibility tests are retired.
It is not the production default and is not read-only: explicitly selecting
`AGENT_FRAMEWORK=native` makes it a fully writable compatibility
path, and its Engineer batch/file-size policy and Reviewer repair-scope rules
apply only there. No new features are added to it.

## Local development

Use a Python 3.11 interpreter:

```bash
cd services/control-plane
uv sync --extra dev
```

For API-only work, SQLite is sufficient:

```bash
export DATABASE_URL='sqlite+aiosqlite:///./fomo.db'
uv run fomo-api
```

Run the worker in another terminal. A real user-facing run uses LiteLLM and the
local OpenSandbox Server started by Compose. Set `OPENSANDBOX_BASE_URL`,
`OPENSANDBOX_API_KEY`, and optionally `OPENSANDBOX_IMAGE` through the shell or
your deployment secret manager; this application never reads dotenv files. For
trusted local development only, the process adapter can exercise the entire
file/command/Git/QA path:

```bash
export SANDBOX_PROVIDER=process
export ALLOW_UNSAFE_PROCESS_SANDBOX=true
export LITELLM_BASE_URL=http://localhost:4000/v1
uv run fomo-worker
```

The worker never starts a host process unless that explicit opt-in is present.
The FOMO system `.gitignore` baseline is owned by the control plane and cannot
be weakened by agent output.

For a controlled public deployment, set `PUBLIC_PREVIEW_BASE_DOMAIN` and run
`fomo-preview-gateway` (the Compose `public-preview` profile exposes it only on
loopback port 8001). Final publication stores the wildcard HTTPS URL, while QA
continues to verify the direct internal endpoint first. The gateway accepts
only canonical sandbox UUID hosts backed by a current, uncleaned verified
resource, strips control-plane credentials/cookies, and maps confirmed provider
expiry to durable `preview.expired` state. It is an HTTP proxy; generated apps
that require WebSocket, SSE, or streaming transport are outside the current
demo contract.

The gateway service must use a minimal environment: `DATABASE_URL`,
`OPENSANDBOX_BASE_URL`, `OPENSANDBOX_API_KEY`,
`PUBLIC_PREVIEW_BASE_DOMAIN`, `PREVIEW_UPSTREAM_HOST_OVERRIDE`, and
`PREVIEW_GATEWAY_PORT` only. It must not inherit the API/worker environment or
receive LiteLLM/model, Redis, MinIO, or AWS credentials.

The named-Tunnel example in
[`deploy/cloudflared/config.example.yml`](../../deploy/cloudflared/config.example.yml)
keeps the workbench and generated Preview on different eTLD+1 sites:
`app.example.com` and `*.fomo-previews.example.net`. Preserve the incoming
Preview Host, bypass edge caching for that wildcard, provision TLS for the
exact wildcard depth, and leave the final `http_status:404` ingress rule in
place. The production origin exposes no inbound port when using Tunnel; at
most, a non-Tunnel deployment exposes its TLS ingress. OpenSandbox `8080`,
LiteLLM `4000`, random Preview host ports including `40000-60000`, PostgreSQL,
Redis, and MinIO must never be publicly reachable.

Retention is a single bounded seven-day renewal, not indefinite hosting. Keep
the host services and Tunnel alive and renew or rerun before the review window
expires. A local OpenSandbox/gateway Playwright pass is not public evidence;
public acceptance additionally requires an external-network HTTPS run through
DNS/TLS/Tunnel, account authentication, API SSE, generated Preview assets,
interaction, and reload persistence.

## API slice

- `POST /v1/sessions/guest`
- `GET|POST /v1/projects`, `GET|PATCH /v1/projects/{projectId}`
- `POST /v1/projects/{projectId}/messages` (idempotent, returns a queued run)
- `GET /v1/runs/{runId}`, `GET /v1/runs/{runId}/events`, `POST .../cancel`
- `GET /v1/projects/{projectId}` returns a refresh-safe baseline: active run,
  last sequence, and replayable events for the active run—or the latest
  terminal run when no run is active—plus file manifest, versions, AC trace
  projection, and preview state.
- `GET|PUT` project file content; `PUT` requires `baseVersionId` plus the file
  `baseSha256` and returns `409` rather than overwriting a newer version.
- `GET` versions, `POST .../versions/{versionId}/restore` (new immutable head),
  `GET .../download?versionId=` (durable source ZIP), trace, and preview
  resources under a project.

SSE events are persisted before delivery, strictly sequenced per run, and can
be replayed using `Last-Event-ID` or `after`. Events only contain visible
activity and bounded/redacted command output—never model chain-of-thought.

## Test

```bash
uv run pytest
uv run ruff check src tests
```

Tests use `ScriptedModelClient`, `FakeSandboxProvider`, and a fake Pi bridge,
so they make no network/model calls and execute no generated host code. Per
`AGENTS.md`, there is no pre-approval gate: verification is designed as one
minimal sufficient matrix per big module and executed centrally, with
necessary targeted regression for high-risk fixes.
