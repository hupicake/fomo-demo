# FOMO Control Plane

FastAPI control plane plus an independent durable worker for FOMO's four-role
coding workflow:

`Product Manager → Architect → Engineer → Reviewer`.

The database owns sessions, projects, runs, durable SSE events, structured
artifacts, trace links, evidence, versions, and text file snapshots. The API
only accepts commands and serves state; generated code and package commands
run in the worker through a `SandboxProvider`.

## Runtime

- Python is deliberately pinned to **3.11** (`>=3.11,<3.12`), matching the
  optional MetaGPT compatibility range.
- LiteLLM is called through its OpenAI-compatible endpoint with logical aliases
  (`MODEL_PM`, `MODEL_ARCHITECT`, `MODEL_ENGINEER`, `MODEL_REVIEWER`). Provider
  credentials stay in LiteLLM and are never read from `.env` files by this app;
  the worker accepts a gateway credential injected as `LITELLM_API_KEY` (or the
  local `LITELLM_MASTER_KEY` compatibility variable) without logging it.
- Each model request has a separate, bounded transport-recovery budget: by
  default it makes two additional attempts for `408`, `429`, `500`, `502`,
  `503`, `504`, connection/read timeouts, or protocol resets. It uses capped
  exponential backoff and a reasonable `Retry-After` value when supplied.
  Configure it with `MODEL_NETWORK_RETRIES`,
  `MODEL_NETWORK_RETRY_BASE_DELAY_SECONDS`,
  `MODEL_NETWORK_RETRY_MAX_DELAY_SECONDS`, and
  `MODEL_RETRY_AFTER_MAX_SECONDS`. This is independent from
  `STRUCTURED_OUTPUT_RETRIES`, which is reserved for model JSON/schema repair;
  exhausted gateway failures do not spend that schema-repair budget, and
  ordinary 4xx responses are not transport-retried. Safe `agent.activity`
  retry events expose only attempt count, bounded delay, and HTTP/transport
  class—never request URLs, headers, bodies, or keys. Model request timeout is
  separately configured with `MODEL_REQUEST_TIMEOUT_SECONDS` (default `300`),
  rather than sharing the sandbox command timeout.
- Engineer implementation is deliberately bounded: a real Engineer
  `Role`/`Action` first produces an `ImplementationPlan`, then emits complete
  files in `implementation_batch` artifacts (defaults: at most 24 batches, 1
  file, a 12,000-character split target, and a 20,000-character hard limit per
  create/modify file). Architect and Engineer prompts favor splitting at the
  target; only content above the hard limit is rejected. A successfully
  persisted batch over the target but within the hard limit emits one safe
  `file_batch_over_target` activity with aggregate counts only. Tune the
  limits with `ENGINEER_MAX_BATCHES`, `ENGINEER_MAX_FILES_PER_BATCH`,
  `ENGINEER_TARGET_FILE_CHARACTERS`, and `ENGINEER_MAX_FILE_CHARACTERS`; both
  file-character values must be positive, target must not exceed hard, and hard
  must not exceed 24,000.
  Batches are durable evidence within the current run, not cross-run resume
  checkpoints: terminal failure/cancellation still safely destroys the sandbox.
- `AGENT_FRAMEWORK=metagpt` is the default production coordination layer and
  uses the VCS-SHA-pinned MetaGPT extra. It instantiates four real custom
  `Role`/`Action` pairs (`FomoProductManagerRole` /
  `FomoProductManagerAction`, `FomoArchitectRole` /
  `FomoArchitectAction`, `FomoEngineerRole` / `FomoEngineerAction`, and
  `FomoReviewerRole` / `FomoReviewerAction`) and real MetaGPT `Message`
  hand-offs.
  Each action calls FOMO's `ModelClient` and immediately Pydantic-validates its
  assigned artifact; it never calls MetaGPT's configured model or `Team` /
  repository-generation helpers.
- Before importing MetaGPT, FOMO bootstraps an internal, non-secret minimal
  `config2.yaml` so the pinned package can construct `Role` and `Action`
  safely. This is not a user configuration requirement and no provider request
  is made through it; LiteLLM remains the only generation path.
- MetaGPT owns collaboration primitives only. FOMO's `SOPRunner` remains the
  authority for phase transitions, retry policy, artifact and evidence
  persistence, sandbox/tool permissions, deterministic QA, Git commits, and
  version publishing. Inter-role MetaGPT messages carry only a persisted
  artifact ID, kind, role, and bounded summary—never raw artifact JSON.
- MetaGPT's diagnostic Loguru sinks are disabled because their traceback mode
  can serialize frame locals. A structured model failure returns a controlled
  MetaGPT message first, then is re-raised at FOMO's retry boundary; request
  headers, bodies, and gateway keys are never emitted by that path.
- The MetaGPT extra is mandatory when the default framework is selected. A
  missing or unloadable extra fails worker construction with an install command;
  there is no silent fake/native fallback. `AGENT_FRAMEWORK=native` is an
  explicit test or diagnostic mode only.
- Production/default sandbox selection is `opensandbox`, implemented against
  **OpenSandbox Server v0.2.2** with the pinned **Python SDK v0.1.15**. It
  creates an arm64 workspace from the curated
  `fomo-sandbox-node:2026-08-08` base image (or `OPENSANDBOX_IMAGE`), streams execd command
  output, supports file reads/writes, lifecycle pause/kill, and maps previews
  to `get_endpoint(8080)`. Port `44772` is `execd`, is never a preview, and is
  never returned to a browser. FOMO intentionally uses Git commits plus its
  file manifest for versions; server snapshot rollback is not enabled yet.
- Generated-code sandboxes do not inherit the worker's `HTTP_PROXY` /
  `HTTPS_PROXY`. To allow package installation through a local proxy, set only
  `SANDBOX_HTTP_PROXY`, `SANDBOX_HTTPS_PROXY`, and/or `SANDBOX_NO_PROXY`; they
  become the sandbox's `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` only when
  nonempty. Proxy URLs must be `http(s)` without userinfo. Model, platform,
  and gateway credentials are never injected into sandbox environments.
- `ProcessSandboxProvider` is **only** for trusted local development/CI and
  requires `ALLOW_UNSAFE_PROCESS_SANDBOX=true`; it is not a fallback for
  OpenSandbox and is not safe for public user input.

## Local development

Use a Python 3.11 interpreter:

```bash
cd services/control-plane
uv sync --extra metagpt --extra dev
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
Generated projects must provide `pnpm` scripts named `typecheck`, `build`, and
`dev`; preview is started with:

```text
pnpm dev --hostname 0.0.0.0 --port 8080
```

Before every candidate commit, the SOP owns a baseline `.gitignore` (including
`node_modules`, `.next`, build output, coverage, and Playwright artifacts) and
the persisted file manifest applies the same exclusions. Agent output cannot
delete or weaken that safety baseline.

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

Tests use `ScriptedModelClient` and `FakeSandboxProvider`, so they make no
network/model calls and execute no generated host code.
