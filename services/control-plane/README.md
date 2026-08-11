# FOMO Control Plane

FastAPI control plane plus an independent durable worker for FOMO's coding
agent runtimes. Each run freezes `pi`, `opencode`, or `codex`; all execute inside an
OpenSandbox generation sandbox and reuse the same GoalGraph, safety audit,
clean verification and publication pipeline. GoalGraph is the sole planning
and execution model.

**Status (honest):** GoalGraph, goal-scoped
acceptance, durable checkpoints, recovery, selectable model runtime contracts,
and authenticated account/session isolation are implemented. A standalone
Context Inspector, semantic Verified Reuse registry, same-condition A/B
benchmark, and public HTTPS acceptance remain incomplete. Current release
verification results are recorded in the repository-level `SUBMISSION.md`;
code tests, local OpenSandbox/Chrome checks, and public DNS/TLS/Tunnel checks
remain distinct evidence levels.

The database owns sessions, projects, runs, durable SSE events, structured
artifacts, trace links, evidence, versions, and text file snapshots. The API
only accepts commands and serves state; generated code and package commands
run in the worker through a `SandboxProvider`.

## Runtime

- `FOMO_AGENT_ENABLED_FRAMEWORKS` controls the `pi,opencode,codex` allowlist and
  `FOMO_AGENT_DEFAULT_FRAMEWORK` controls the default. The browser submits a
  public framework id; the server freezes it on the Run and workers fail closed
  if the selected adapter is unavailable.
- OpenCode is pinned in the sandbox image and accessed through a loopback-only
  SDK server. Its bridge maps sessions, structured planning, tools, usage and
  cancellation into FOMO's durable event contract; OpenSandbox remains the
  hard process/filesystem boundary.
- Codex CLI is pinned in the same sandbox image and accessed through its JSONL
  `exec`/`resume` protocol. The server only permits GPT-5.5 or GPT-5.6 profiles;
  a root-owned adapter maps its session, tool and usage events into the same
  durable contract without exposing provider credentials.

- Python is pinned to **3.11** (`>=3.11,<3.12`).
- Direct Pi model egress goes through LiteLLM with a per-run opaque virtual
  key (`LiteLLMRunKeyClient`). Public model profiles are restricted by
  `FOMO_RUNTIME_ENABLED_PROFILES` (default: `deepseek-flash`), discovered
  aliases, and explicit thinking compatibility with **no silent fallback**.
  Legacy `fomo-pi-flash` remains only for persisted-run compatibility.
  Provider credentials stay in
  LiteLLM and never enter a sandbox.
- An uninterrupted run reuses one `fomo-pi-ds` session across planning,
  multiple GoalGraph goals, and repair turns. Recovery never trusts an orphan
  process: it creates a fresh session from the latest verified checkpoint and
  persisted usage balance. GoalGraph is authoritative. BUILDING uses Pi's native
  `read/write/edit/bash` tools and **no business-file write allowlist**. The
  Base Snapshot (starter base + capabilities + prior verified state) is
  mutable: package.json, lockfiles, config, routes, app shell, components, and
  ordinary tests may all be added, moved, modified, or deleted.
- The settle audit (`direct_pi/workspace.py`) enforces only real safety
  invariants: normalized in-workspace paths; `.env*` files are rejected
  outright; `.git/**` (the G-internal checkpoint) is excluded; no
  symlinks/devices or non-regular files; only real changed/new files enter
  the candidate diff (`pnpm-lock.yaml` allowed up to the 512 KiB persistence
  limit); no business-file count or ordinary source-size development quota;
  FOMO-owned roots
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
- A/B telemetry is emitted by production events (`coding_agent.tool.*`/
  `coding_agent.completed`, `preview.available/verified` elapsedSeconds);
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

Run the worker in another terminal. The repository default Direct Pi profile
uses the DeepSeek-backed canonical alias; override it to a route you have
actually verified. GPT profiles use `OPENAI_API_KEY`, Grok uses
`GROK_API_KEY`, and Kimi/Gemini use `OPENCODE_API_KEY`. This application never
reads dotenv files itself; for a host process, load the repository environment
explicitly and run the bounded preflight before the worker:

```bash
cd ../..
uv run --env-file .env.local --project services/control-plane fomo-runtime-preflight
uv run --env-file .env.local --project services/control-plane fomo-worker
```

The preflight creates a short-lived OpenSandbox and proves one bounded streamed
function call through every enabled alias via `SANDBOX_LITELLM_BASE_URL`; it
then revokes the temporary key and destroys the sandbox. Optional profiles must
be added explicitly to `FOMO_RUNTIME_ENABLED_PROFILES` only after their real
credential is configured. A failing enabled route blocks worker startup and
cannot silently fall back. The canary can incur a very small provider charge.
The FOMO system `.gitignore` baseline is owned by the control plane and cannot
be weakened by agent output.

For a controlled public deployment, run `fomo-preview-gateway` (the Compose
`public-preview` profile exposes it only on loopback port 8001). Configure either
`PUBLIC_PREVIEW_BASE_URL` for a path URL or the stronger isolated
`PUBLIC_PREVIEW_BASE_DOMAIN` wildcard mode; production defaults to
`WEB_ORIGIN/preview` when both are unset. A URL on the same registrable site as
`WEB_ORIGIN` keeps the opaque CSP sandbox without storage/forms. A fixed URL on
a different registrable site (including a dedicated authenticated tunnel)
permits hydration, forms, and localStorage, is framed only by `WEB_ORIGIN`, and
disables workers. Resource, connection, and form CSP paths are defense in depth,
not isolation: URL redirects can invalidate path assumptions. All Preview IDs
share that fixed URL's browser origin and storage, so URL mode is for a trusted
single-tenant demo only. A non-loopback URL must use HTTPS; a URL on the same
registrable site must use the exact `WEB_ORIGIN`, not another subdomain or port.
QA continues to verify the direct
internal endpoint first. The gateway requires an exact canonical UUID and
verified persisted URL, strips credentials/cookies, adds no-store, and maps
confirmed provider expiry to durable `preview.expired` state. It is an HTTP proxy; generated apps
that require WebSocket, SSE, or streaming transport are outside the current
demo contract.

The gateway service must use a minimal environment: `DATABASE_URL`,
`APP_ENV`, `WEB_ORIGIN`, `OPENSANDBOX_BASE_URL`, `OPENSANDBOX_API_KEY`,
one public Preview setting, `PREVIEW_UPSTREAM_HOST_OVERRIDE`, and
`PREVIEW_GATEWAY_PORT` only. It must not inherit the API/worker environment or
receive LiteLLM/model or unrelated worker credentials.

The named-Tunnel example in
[`deploy/cloudflared/config.example.yml`](../../deploy/cloudflared/config.example.yml)
keeps the workbench and generated Preview on different eTLD+1 sites:
`app.example.com` and `*.fomo-previews.example.net`. Preserve the incoming
Preview Host, bypass edge caching for that wildcard, provision TLS for the
exact wildcard depth, and leave the final `http_status:404` ingress rule in
place. The production origin exposes no inbound port when using Tunnel; at
most, a non-Tunnel deployment exposes its TLS ingress. OpenSandbox `8080`,
LiteLLM `4000`, random Preview host ports including `40000-60000`, PostgreSQL,
and other internal service ports must never be publicly reachable.

Retention is a single bounded seven-day renewal, not indefinite hosting. Keep
the host services and Tunnel alive and renew or rerun before the review window
expires. A local OpenSandbox/gateway Playwright pass is not public evidence;
public acceptance additionally requires an external-network HTTPS run through
DNS/TLS/Tunnel, account authentication, API SSE, generated Preview assets,
interaction, and reload persistence.

## API slice

- `POST /v1/auth/register`, `POST /v1/auth/login`, `GET /v1/auth/me`, and
  `POST /v1/auth/logout`; browser authentication uses the host-only HttpOnly
  session cookie and never exposes the session token in JSON.
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

Tests use `FakeSandboxProvider` and fake coding-agent transports,
so they make no network/model calls and execute no generated host code. Per
`AGENTS.md`, there is no pre-approval gate: verification is designed as one
minimal sufficient matrix per big module and executed centrally, with
necessary targeted regression for high-risk fixes.
