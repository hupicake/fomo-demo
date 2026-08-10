#!/usr/bin/env node
/**
 * Trusted foreground bridge between OpenSandbox and Pi RPC.
 *
 * The bridge owns Pi stdin/stdout for one turn. A later repair turn starts a
 * new bridge process with the same session id; the session is only a cache.
 * stdout is reserved for the versioned FOMO JSONL protocol. Secrets and Pi
 * stderr never enter that protocol.
 *
 * Scope (P0): transport, event/usage observation, cancellation, total
 * resource liveness, redaction, fail-closed protocol, and
 * session reuse. Build and repair turns keep Pi's official builtin tools with
 * full /workspace permission. Planning turns may instead expose one trusted,
 * schema-backed terminating tool; the bridge never proxies filesystem tool
 * semantics or enforces business-file write allowlists.
 */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { TextDecoder } from "node:util";

const SCHEMA_VERSION = 1;
const PROVIDER_ID = "fomo-litellm";
const DEFAULT_MODEL_REF = `${PROVIDER_ID}/fomo-pi-flash`;

function runtimeModel(
  id,
  thinkingLevels,
  thinkingLevelMap,
  {
    format = "openai",
    replayReasoning = false,
    supportsReasoningEffort = true,
    fixedThinkingLevel = null,
    maxContextWindow = 250_000,
    maxOutputTokens = 128_000,
  } = {},
) {
  return Object.freeze({
    id,
    thinkingLevels: Object.freeze([...thinkingLevels]),
    thinkingLevelMap: Object.freeze({ ...thinkingLevelMap }),
    fixedThinkingLevel,
    maxContextWindow,
    maxOutputTokens,
    compat: Object.freeze({
      supportsDeveloperRole: false,
      supportsReasoningEffort,
      requiresReasoningContentOnAssistantMessages: replayReasoning,
      thinkingFormat: format,
      maxTokensField: "max_tokens",
    }),
  });
}

const MODEL_CONFIGS = Object.freeze({
  // Historical aliases remain available only for pre-0005 run resumption.
  [`${PROVIDER_ID}/fomo-pi-flash`]: runtimeModel(
    "fomo-pi-flash", ["off", "high", "max"],
    { minimal: null, low: null, medium: null, high: "high", xhigh: null, max: "max" },
    {
      format: "deepseek",
      replayReasoning: true,
      maxContextWindow: 1_000_000,
      maxOutputTokens: 384_000,
    },
  ),
  [`${PROVIDER_ID}/fomo-pi-build`]: runtimeModel(
    "fomo-pi-build", ["off", "medium", "high"],
    { minimal: null, low: null, medium: "medium", high: "high", xhigh: null, max: null },
  ),
  [`${PROVIDER_ID}/fomo-pi-gpt-5.6`]: runtimeModel(
    "fomo-pi-gpt-5.6", ["off", "low", "medium", "high", "xhigh", "max"],
    { minimal: null, low: "low", medium: "medium", high: "high", xhigh: "xhigh", max: "max" },
  ),
  [`${PROVIDER_ID}/fomo-pi-gpt-5.5`]: runtimeModel(
    "fomo-pi-gpt-5.5", ["off", "low", "medium", "high", "xhigh"],
    { minimal: null, low: "low", medium: "medium", high: "high", xhigh: "xhigh", max: null },
  ),
  [`${PROVIDER_ID}/fomo-pi-deepseek-flash`]: runtimeModel(
    "fomo-pi-deepseek-flash", ["off", "high"],
    { minimal: null, low: null, medium: null, high: "high", xhigh: null, max: null },
    {
      format: "deepseek",
      replayReasoning: true,
      maxContextWindow: 1_000_000,
      maxOutputTokens: 384_000,
    },
  ),
  [`${PROVIDER_ID}/fomo-pi-grok-4.5`]: runtimeModel(
    "fomo-pi-grok-4.5", ["low", "medium", "high"],
    { minimal: null, low: "low", medium: "medium", high: "high", xhigh: null, max: null },
    { maxContextWindow: 500_000, maxOutputTokens: 500_000 },
  ),
  [`${PROVIDER_ID}/fomo-pi-kimi-k2.7-code`]: runtimeModel(
    "fomo-pi-kimi-k2.7-code", ["default"],
    { minimal: null, low: null, medium: null, high: null, xhigh: null, max: null },
    {
      format: "deepseek",
      replayReasoning: true,
      supportsReasoningEffort: false,
      fixedThinkingLevel: "off",
      maxContextWindow: 262_144,
      maxOutputTokens: 262_144,
    },
  ),
  [`${PROVIDER_ID}/fomo-pi-gemini-3.6-flash`]: runtimeModel(
    "fomo-pi-gemini-3.6-flash", ["minimal", "low", "medium", "high"],
    { minimal: "minimal", low: "low", medium: "medium", high: "high", xhigh: null, max: null },
    { maxOutputTokens: 65_536 },
  ),
  [`${PROVIDER_ID}/fomo-pi-gemini-3.1-pro`]: runtimeModel(
    "fomo-pi-gemini-3.1-pro", ["low", "medium", "high"],
    { minimal: null, low: "low", medium: "medium", high: "high", xhigh: null, max: null },
    { maxOutputTokens: 65_536 },
  ),
});
const DEFAULT_THINKING_LEVEL = "max";
const DEFAULT_CONTEXT_WINDOW = 200_000;
const MAX_CONTEXT_WINDOW = 1_000_000;
const COMPACTION_SETTINGS = Object.freeze({
  enabled: true,
  reserveTokens: 32_768,
  keepRecentTokens: 20_000,
});
// Official Pi v0.84.1 builtin tools. The bridge mirrors this list as the
// fail-closed transport contract; it never intercepts or rewrites tool calls.
const BUILTIN_TOOLS = "read,write,edit,bash,grep,find,ls";
const DELEGATE_SUBTASKS_TOOL = "delegate_subtasks";
const DELEGATE_SUBTASKS_EXTENSION = fileURLToPath(
  new URL("./fomo-delegate-subtasks.ts", import.meta.url),
);
const STRUCTURED_OUTPUT_TOOL = "submit_structured_output";
const STRUCTURED_OUTPUT_EXTENSION = fileURLToPath(new URL("./fomo-structured-output.ts", import.meta.url));
const USER_INPUT_TOOL = "request_user_input";
const MAX_USER_INPUT_ATTEMPTS = 3;
const USER_INPUT_EXTENSION = fileURLToPath(new URL("./fomo-request-user-input.ts", import.meta.url));

const ENV = Object.freeze({
  prompt: "FOMO_PI_PROMPT_B64",
  sessionId: "FOMO_PI_SESSION_ID",
  requestId: "FOMO_PI_REQUEST_ID",
  correlationId: "FOMO_PI_CORRELATION_ID",
  providerBaseUrl: "FOMO_PI_PROVIDER_BASE_URL",
  virtualKey: "FOMO_PI_VIRTUAL_KEY",
  workspace: "FOMO_PI_WORKSPACE",
  stateDir: "FOMO_PI_STATE_DIR",
  piBin: "FOMO_PI_BIN",
  thinkingLevel: "FOMO_PI_THINKING_LEVEL",
  effectiveThinkingLevel: "FOMO_PI_EFFECTIVE_THINKING_LEVEL",
  modelRef: "FOMO_PI_MODEL_REF",
  contextWindow: "FOMO_PI_CONTEXT_WINDOW",
  activitySilenceSeconds: "FOMO_PI_ACTIVITY_SILENCE_SECONDS",
  structuredOutputSchema: "FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64",
  userInputEnabled: "FOMO_PI_USER_INPUT_ENABLED",
  requireResume: "FOMO_PI_REQUIRE_RESUME",
  timeoutSeconds: "FOMO_PI_TIMEOUT_SECONDS",
  graceSeconds: "FOMO_PI_GRACE_SECONDS",
});

const DEFAULTS = Object.freeze({
  workspace: "/workspace",
  stateDir: "/var/lib/fomo-pi",
  piBin: "/opt/fomo/pi/bin/pi",
  graceSeconds: 10,
});

const LIMITS = Object.freeze({
  promptCharacters: 100_000,
  identifierCharacters: 128,
  keyCharacters: 4096,
  lineBytes: 16 * 1024 * 1024,
  totalStdoutBytes: 256 * 1024 * 1024,
  stderrBytes: 64 * 1024,
  publicTextCharacters: 8192,
  publicDeltaCharacters: 4096,
  publicArgumentCharacters: 2048,
  structuredOutputSchemaBytes: 64 * 1024,
  userInputQuestionCharacters: 2000,
  userInputChoiceCharacters: 200,
  userInputChoices: 8,
  userInputReasonCharacters: 1000,
  delegatedTasks: 3,
  delegatedTaskIdCharacters: 40,
  delegatedTaskCharacters: 2000,
  arrayItems: 128,
});

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
const SESSION_ID = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/;
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;
const PUBLIC_PI_KINDS = new Set([
  "agent_start",
  "agent_end",
  "agent_settled",
  "turn_start",
  "turn_end",
  "message_start",
  "message_delta",
  "message_end",
  "bash_output",
  "tool_start",
  "tool_output",
  "tool_end",
  "queue_update",
  "compaction_start",
  "compaction_end",
  "auto_retry_start",
  "auto_retry_end",
  "summarization_retry_scheduled",
  "summarization_retry_attempt_start",
  "summarization_retry_finished",
  "extension_error",
  "input_request",
  "inference_heartbeat",
]);

let requestId = safeIdentifier(process.env[ENV.requestId], "invalid-request");
let correlationId = safeIdentifier(process.env[ENV.correlationId], "invalid-correlation");
let sessionId = "";
let prompt = "";
let promptBase64 = "";
let virtualKey = "";
let providerBaseUrl = "";
let workspace = DEFAULTS.workspace;
let stateDir = DEFAULTS.stateDir;
let piBin = DEFAULTS.piBin;
let thinkingLevel = DEFAULT_THINKING_LEVEL;
let effectiveThinkingLevel = DEFAULT_THINKING_LEVEL;
let modelRef = DEFAULT_MODEL_REF;
let modelConfig = MODEL_CONFIGS[DEFAULT_MODEL_REF];
// Explicit FOMO logical context window. The active provider supports a larger
// window, while this lower product budget bounds latency and compaction.
let contextWindow = DEFAULT_CONTEXT_WINDOW;
let activitySilenceSeconds = null;
let structuredOutputSchemaBase64 = "";
let structuredOutputMode = false;
let userInputEnabled = false;
let requireResume = false;
let activeTools = BUILTIN_TOOLS;
let allowedToolNames = new Set(BUILTIN_TOOLS.split(","));
let timeoutSeconds = null;
let graceSeconds = DEFAULTS.graceSeconds;

let sequence = 0;
let lifecycle = "booting";
let child = null;
let childExited = false;
let childExitCode = null;
let childExitSignal = null;
let stdoutEnded = false;
let stdoutBuffer = Buffer.alloc(0);
let stdoutBytes = 0;
let stderrBytes = 0;
let promptAccepted = false;
let timeoutHandle = null;
let activityTimerHandle = null;
let lastAgentActivityAt = 0;
let lastInferenceHeartbeatAt = 0;
let privateAgentDir = null;
let shuttingDown = false;

// A/B telemetry (persisted through pi.tool.* and pi.completed events):
// first tool and first edit/write tool relative to the started envelope,
// per-tool counts, and the final assistant stop reason. This is measured on
// edit/write tool completions only; the bridge does not interpret tool
// semantics, so bash-side writes are intentionally not counted.
let runStartedAt = 0;
let firstToolElapsedMs = null;
let firstEditOrWriteToolElapsedMs = null;
let lastStopReason = "";
let structuredOutputCalls = 0;
let structuredOutputSuccesses = 0;
let userInputCalls = 0;
let userInputSuccesses = 0;
let inputRequest = null;
const toolCounts = {};
let delegatedTaskCount = 0;
let delegatedChildTaskCount = 0;
let delegatedChildToolCalls = 0;
let delegatedChildTurns = 0;
const delegatedUsage = {
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  cost: 0,
};
const delegatedTaskIdsByCallId = new Map();

const pendingResponses = new Map();
const initial = { modelSelected: false, thinkingSelected: false, state: null, stats: null };
const final = { state: null, stats: null };
const activeToolCalls = new Map();
const completedToolCallIds = new Set();
const userInputArgumentsByCallId = new Map();
const fatalUtf8 = new TextDecoder("utf-8", { fatal: true });

function safeIdentifier(value, fallback) {
  return typeof value === "string" && value.length <= LIMITS.identifierCharacters && IDENTIFIER.test(value)
    ? value
    : fallback;
}

function bounded(value, limit) {
  const text = String(value ?? "");
  return text.length <= limit ? text : `${text.slice(0, limit)}…[truncated]`;
}

function redact(value) {
  let text = String(value ?? "");
  for (const secret of [virtualKey, prompt, promptBase64, structuredOutputSchemaBase64]) {
    if (secret) text = text.split(secret).join("[redacted]");
  }
  return text;
}

function emit(type, payload = {}) {
  sequence += 1;
  process.stdout.write(`${JSON.stringify({
    schemaVersion: SCHEMA_VERSION,
    requestId,
    correlationId,
    seq: sequence,
    type,
    payload,
  })}\n`);
}

function diagnostic(message) {
  if (stderrBytes >= LIMITS.stderrBytes) return;
  const text = `[fomo-pi-bridge] ${bounded(redact(message), 2048)}\n`;
  stderrBytes += Buffer.byteLength(text);
  if (stderrBytes <= LIMITS.stderrBytes) process.stderr.write(text);
}

function required(name) {
  const value = process.env[name];
  if (typeof value !== "string" || value === "") throw new Error(`missing ${name}`);
  return value;
}

function parsePositiveInteger(name, value, maximum = Number.MAX_SAFE_INTEGER) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw new Error(`${name} must be an integer between 1 and ${maximum}`);
  }
  return parsed;
}

function validateAbsolutePath(name, value) {
  if (!value.startsWith("/") || value.includes("\0")) throw new Error(`${name} must be an absolute path`);
  return value;
}

function parseStructuredOutputSchema() {
  structuredOutputSchemaBase64 = process.env[ENV.structuredOutputSchema] || "";
  if (!structuredOutputSchemaBase64) return;

  const maximumBase64Characters = Math.ceil(LIMITS.structuredOutputSchemaBytes / 3) * 4;
  if (
    structuredOutputSchemaBase64.length > maximumBase64Characters ||
    structuredOutputSchemaBase64.length % 4 !== 0 ||
    !BASE64.test(structuredOutputSchemaBase64)
  ) {
    throw new Error(`${ENV.structuredOutputSchema} must be bounded canonical base64`);
  }

  const schemaBytes = Buffer.from(structuredOutputSchemaBase64, "base64");
  if (
    !schemaBytes.length ||
    schemaBytes.length > LIMITS.structuredOutputSchemaBytes ||
    schemaBytes.toString("base64") !== structuredOutputSchemaBase64
  ) {
    throw new Error(
      `${ENV.structuredOutputSchema} must decode to at most ${LIMITS.structuredOutputSchemaBytes} bytes`,
    );
  }

  let schemaText;
  try {
    schemaText = fatalUtf8.decode(schemaBytes);
  } catch {
    throw new Error(`${ENV.structuredOutputSchema} must contain UTF-8 JSON`);
  }

  let schema;
  try {
    schema = JSON.parse(schemaText);
  } catch {
    throw new Error(`${ENV.structuredOutputSchema} must contain valid JSON`);
  }
  if (!schema || typeof schema !== "object" || Array.isArray(schema) || schema.type !== "object") {
    throw new Error(`${ENV.structuredOutputSchema} must contain a root object JSON Schema`);
  }
  if (!existsSync(STRUCTURED_OUTPUT_EXTENSION) || !statSync(STRUCTURED_OUTPUT_EXTENSION).isFile()) {
    throw new Error("trusted structured-output extension is unavailable");
  }

  structuredOutputMode = true;
  activeTools = STRUCTURED_OUTPUT_TOOL;
  allowedToolNames = new Set([STRUCTURED_OUTPUT_TOOL]);
}

function parseFeatureFlag(name) {
  const value = process.env[name];
  if (value === undefined || value === "") return false;
  if (value !== "1") throw new Error(`${name} must be 1 when enabled`);
  return true;
}

function configureDelegateSubtasksTool() {
  if (structuredOutputMode || process.env.FOMO_PI_DELEGATION_CHILD === "1") return;
  if (
    !existsSync(DELEGATE_SUBTASKS_EXTENSION) ||
    !statSync(DELEGATE_SUBTASKS_EXTENSION).isFile()
  ) {
    throw new Error("trusted read-only delegation extension is unavailable");
  }
  activeTools = `${activeTools},${DELEGATE_SUBTASKS_TOOL}`;
  allowedToolNames.add(DELEGATE_SUBTASKS_TOOL);
}

function configureUserInputTool() {
  userInputEnabled = parseFeatureFlag(ENV.userInputEnabled);
  requireResume = parseFeatureFlag(ENV.requireResume);
  if (!userInputEnabled) return;
  if (!existsSync(USER_INPUT_EXTENSION) || !statSync(USER_INPUT_EXTENSION).isFile()) {
    throw new Error("trusted user-input extension is unavailable");
  }
  activeTools = `${activeTools},${USER_INPUT_TOOL}`;
  allowedToolNames.add(USER_INPUT_TOOL);
}

function parseEnvironment() {
  requestId = required(ENV.requestId);
  correlationId = required(ENV.correlationId);
  for (const [name, value] of [[ENV.requestId, requestId], [ENV.correlationId, correlationId]]) {
    if (value.length > LIMITS.identifierCharacters || !IDENTIFIER.test(value)) {
      throw new Error(`${name} is not a valid identifier`);
    }
  }

  sessionId = required(ENV.sessionId);
  if (sessionId.length > LIMITS.identifierCharacters || !SESSION_ID.test(sessionId)) {
    throw new Error(`${ENV.sessionId} is not a valid session id`);
  }

  promptBase64 = required(ENV.prompt);
  if (!BASE64.test(promptBase64) || promptBase64.length % 4 !== 0) {
    throw new Error(`${ENV.prompt} is not canonical base64`);
  }
  const promptBytes = Buffer.from(promptBase64, "base64");
  const canonical = promptBytes.toString("base64");
  if (canonical !== promptBase64) throw new Error(`${ENV.prompt} is not canonical base64`);
  try {
    prompt = fatalUtf8.decode(promptBytes);
  } catch {
    throw new Error(`${ENV.prompt} is not valid UTF-8`);
  }
  if (!prompt.trim() || prompt.length > LIMITS.promptCharacters) {
    throw new Error("prompt must be non-empty and within the character limit");
  }

  virtualKey = required(ENV.virtualKey);
  if (virtualKey.length > LIMITS.keyCharacters || /[\x00-\x1f\x7f]/.test(virtualKey)) {
    throw new Error(`${ENV.virtualKey} is invalid`);
  }

  const rawBaseUrl = required(ENV.providerBaseUrl);
  let parsed;
  try {
    parsed = new URL(rawBaseUrl);
  } catch {
    throw new Error(`${ENV.providerBaseUrl} is not a valid URL`);
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username || parsed.password || parsed.search || parsed.hash ||
    !parsed.hostname || !parsed.pathname.replace(/\/+$/, "").endsWith("/v1")
  ) {
    throw new Error(`${ENV.providerBaseUrl} must be an http(s) URL ending in /v1 without userinfo, query, or fragment`);
  }
  parsed.pathname = parsed.pathname.replace(/\/+$/, "");
  providerBaseUrl = parsed.toString().replace(/\/$/, "");

  workspace = validateAbsolutePath(ENV.workspace, process.env[ENV.workspace] || DEFAULTS.workspace);
  stateDir = validateAbsolutePath(ENV.stateDir, process.env[ENV.stateDir] || DEFAULTS.stateDir);
  piBin = validateAbsolutePath(ENV.piBin, process.env[ENV.piBin] || DEFAULTS.piBin);
  modelRef = process.env[ENV.modelRef] || DEFAULT_MODEL_REF;
  modelConfig = MODEL_CONFIGS[modelRef];
  if (!modelConfig) {
    throw new Error(`${ENV.modelRef} must select a supported FOMO Pi model`);
  }
  thinkingLevel = process.env[ENV.thinkingLevel] || DEFAULT_THINKING_LEVEL;
  if (!modelConfig.thinkingLevels.includes(thinkingLevel)) {
    throw new Error(`${ENV.thinkingLevel} is unsupported by ${modelRef}`);
  }
  effectiveThinkingLevel = modelConfig.fixedThinkingLevel || thinkingLevel;
  if (!existsSync(workspace) || !statSync(workspace).isDirectory()) throw new Error(`${ENV.workspace} is not a directory`);
  if (!existsSync(piBin) || !statSync(piBin).isFile()) throw new Error(`${ENV.piBin} is not a file`);

  if (process.env[ENV.contextWindow]) {
    // Sane explicit bound: a provider alias must not declare an absurd window.
    contextWindow = parsePositiveInteger(
      ENV.contextWindow,
      process.env[ENV.contextWindow],
      modelConfig.maxContextWindow,
    );
  }
  if (process.env[ENV.activitySilenceSeconds]) {
    activitySilenceSeconds = parsePositiveInteger(
      ENV.activitySilenceSeconds, process.env[ENV.activitySilenceSeconds], 3_600,
    );
  }
  parseStructuredOutputSchema();
  configureDelegateSubtasksTool();
  configureUserInputTool();

  if (process.env[ENV.timeoutSeconds]) {
    timeoutSeconds = parsePositiveInteger(ENV.timeoutSeconds, process.env[ENV.timeoutSeconds]);
  }
  if (process.env[ENV.graceSeconds]) {
    graceSeconds = parsePositiveInteger(ENV.graceSeconds, process.env[ENV.graceSeconds], 60);
  }
}

function writePrivateConfiguration() {
  privateAgentDir = join(stateDir, `agent-${randomUUID()}`);
  const sessionsDir = join(stateDir, "sessions");
  mkdirSync(privateAgentDir, { recursive: true, mode: 0o700 });
  mkdirSync(sessionsDir, { recursive: true, mode: 0o700 });
  const models = {
    providers: {
      [PROVIDER_ID]: {
        baseUrl: providerBaseUrl,
        api: "openai-completions",
        apiKey: `$${ENV.virtualKey}`,
        authHeader: true,
        models: [modelConfig].map((config) => ({
          id: config.id,
          name: config.id,
          reasoning: true,
          input: ["text"],
          contextWindow,
          // Provider-specific response maximum with space reserved for the
          // current prompt and Pi's compaction handoff. This is derived from
          // the selected model window, never a unified FOMO quota.
          maxTokens: Math.min(
            modelConfig.maxOutputTokens,
            contextWindow - COMPACTION_SETTINGS.reserveTokens,
          ),
          thinkingLevelMap: config.thinkingLevelMap,
          compat: config.compat,
        })),
      },
    },
  };
  writeFileSync(join(privateAgentDir, "models.json"), `${JSON.stringify(models, null, 2)}\n`, { mode: 0o600 });
  // This bridge-owned ephemeral agent directory is separate from sessionsDir.
  // Recreate the same bounded policy for every foreground process; never place
  // settings in /workspace or the persistent session JSONL directory.
  const settings = { compaction: COMPACTION_SETTINGS };
  writeFileSync(join(privateAgentDir, "settings.json"), `${JSON.stringify(settings, null, 2)}\n`, { mode: 0o600 });
  return sessionsDir;
}

function cleanPrivateConfiguration() {
  if (!privateAgentDir) return;
  try { rmSync(privateAgentDir, { recursive: true, force: true }); } catch { /* best effort */ }
}

function spawnPi(sessionsDir) {
  const environment = { ...process.env };
  delete environment[ENV.prompt];
  environment.PI_CODING_AGENT_DIR = privateAgentDir;
  environment.PI_OFFLINE = "1";
  environment.PI_SKIP_VERSION_CHECK = "1";
  environment.PI_TELEMETRY = "0";
  environment[ENV.effectiveThinkingLevel] = effectiveThinkingLevel;

  const arguments_ = [
    "--mode", "rpc",
    "--session-id", sessionId,
    "--session-dir", sessionsDir,
    "--model", modelRef,
    "--thinking", effectiveThinkingLevel,
    "--tools", activeTools,
    "--no-context-files",
    "--no-extensions",
  ];
  if (allowedToolNames.has(DELEGATE_SUBTASKS_TOOL)) {
    arguments_.push("--extension", DELEGATE_SUBTASKS_EXTENSION);
  }
  if (structuredOutputMode) arguments_.push("--extension", STRUCTURED_OUTPUT_EXTENSION);
  if (userInputEnabled) arguments_.push("--extension", USER_INPUT_EXTENSION);
  arguments_.push(
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-approve",
    "--offline",
  );

  child = spawn(piBin, arguments_, {
    cwd: workspace,
    env: environment,
    stdio: ["pipe", "pipe", "pipe"],
    detached: true,
  });
  child.stdin.on("error", () => {});
  child.on("error", (error) => fail("spawn_failed", `cannot start Pi: ${error.message}`, 1));
  child.stdout.on("data", onPiStdout);
  child.stdout.on("end", onPiStdoutEnd);
  child.stderr.on("data", onPiStderr);
  child.on("exit", onPiExit);
}

function send(id, type, payload = {}) {
  if (!child || child.stdin.destroyed || child.stdin.writableEnded) {
    fail("stdin_closed", "Pi stdin is not writable", 1);
    return;
  }
  const message = { id, type, ...payload };
  if (type === "prompt") message.message = prompt;
  pendingResponses.set(id, type);
  child.stdin.write(`${JSON.stringify(message)}\n`);
}

function onPiStdout(chunk) {
  if (lifecycle === "failed" || lifecycle === "completed") return;
  stdoutBytes += chunk.length;
  if (stdoutBytes > LIMITS.totalStdoutBytes) {
    fail("stdout_too_large", "Pi stdout exceeded its byte limit", 1);
    return;
  }
  stdoutBuffer = Buffer.concat([stdoutBuffer, chunk]);
  let newline = stdoutBuffer.indexOf(0x0a);
  while (newline >= 0) {
    let line = stdoutBuffer.subarray(0, newline);
    stdoutBuffer = stdoutBuffer.subarray(newline + 1);
    if (line.length && line[line.length - 1] === 0x0d) line = line.subarray(0, line.length - 1);
    if (!line.length || line.length > LIMITS.lineBytes) {
      fail("invalid_pi_record", "Pi emitted an empty or oversized JSONL record", 1);
      return;
    }
    let text;
    try { text = fatalUtf8.decode(line); } catch {
      fail("invalid_utf8", "Pi emitted invalid UTF-8", 1);
      return;
    }
    handlePiRecord(text);
    if (lifecycle === "failed") return;
    newline = stdoutBuffer.indexOf(0x0a);
  }
  if (stdoutBuffer.length > LIMITS.lineBytes) fail("line_too_large", "Pi JSONL record exceeds its byte limit", 1);
}

function onPiStdoutEnd() {
  stdoutEnded = true;
  if (stdoutBuffer.length) {
    fail("truncated_pi_record", "Pi stdout ended without a trailing LF", 1);
    return;
  }
  if (lifecycle !== "finalizing" && lifecycle !== "failed") {
    fail("unexpected_eof", "Pi stdout ended before final state and stats", 1);
    return;
  }
  maybeComplete();
}

function onPiStderr(chunk) {
  if (stderrBytes >= LIMITS.stderrBytes) return;
  const text = redact(chunk.toString("utf8"));
  const remaining = LIMITS.stderrBytes - stderrBytes;
  const boundedText = Buffer.from(text).subarray(0, remaining).toString("utf8");
  stderrBytes += Buffer.byteLength(boundedText);
  process.stderr.write(boundedText);
}

function onPiExit(code, signal) {
  childExited = true;
  childExitCode = code;
  childExitSignal = signal;
  if (lifecycle !== "finalizing" && lifecycle !== "failed") {
    fail("unexpected_exit", `Pi exited before finalization (code=${code ?? "null"}, signal=${signal ?? "none"})`, 1);
    return;
  }
  maybeComplete();
}

function handlePiRecord(line) {
  let message;
  try { message = JSON.parse(line); } catch {
    fail("malformed_pi_json", "Pi emitted malformed JSON", 1);
    return;
  }
  if (!message || typeof message !== "object" || Array.isArray(message) || typeof message.type !== "string") {
    fail("malformed_pi_record", "Pi emitted a non-object record without type", 1);
    return;
  }
  if (message.type === "response") handleResponse(message);
  else if (message.type === "extension_ui_request") fail("extension_ui_request", "Pi requested disabled extension UI", 1);
  else handleEvent(message);
}

function handleResponse(message) {
  if (typeof message.id !== "string" || !pendingResponses.has(message.id)) {
    fail("unexpected_response", "Pi response id is missing, duplicate, or unknown", 1);
    return;
  }
  const expectedCommand = pendingResponses.get(message.id);
  pendingResponses.delete(message.id);
  if (message.command !== expectedCommand || message.success !== true) {
    fail("command_failed", `Pi command ${expectedCommand} failed or mismatched`, 1);
    return;
  }
  if (lifecycle === "running") touchAgentActivity();
  if (message.id === "initial-state") initial.state = summarizeState(message.data);
  else if (message.id === "initial-stats") initial.stats = summarizeStats(message.data);
  else if (message.id === "select-model") initial.modelSelected = true;
  else if (message.id === "select-thinking") initial.thinkingSelected = true;
  else if (message.id === "prompt") promptAccepted = true;
  else if (message.id === "final-state") final.state = summarizeState(message.data);
  else if (message.id === "final-stats") final.stats = summarizeStats(message.data);
  else fail("unexpected_response", "Pi response id is not handled", 1);
  if (lifecycle === "booting") maybeStart();
  if (lifecycle === "settled") maybeFinalize();
}

function summarizeState(data) {
  if (!data || typeof data !== "object" || Array.isArray(data) || !data.model || typeof data.model !== "object") {
    fail("invalid_state", "Pi state response is invalid", 1);
    return null;
  }
  if (
    data.model.provider !== PROVIDER_ID ||
    data.model.id !== modelConfig.id ||
    data.thinkingLevel !== effectiveThinkingLevel
  ) {
    fail("model_mismatch", "Pi selected provider, model, or thinking level does not match the run contract", 1);
    return null;
  }
  if (data.sessionId !== sessionId) {
    fail("session_mismatch", "Pi state session id does not match the run contract", 1);
    return null;
  }
  return {
    sessionId,
    messageCount: nonNegativeInteger(data.messageCount, "messageCount"),
    pendingMessageCount: nonNegativeInteger(data.pendingMessageCount, "pendingMessageCount"),
    isStreaming: data.isStreaming === true,
    isCompacting: data.isCompacting === true,
  };
}

function nonNegativeInteger(value, name) {
  if (!Number.isInteger(value) || value < 0) {
    fail("invalid_stats", `${name} must be a non-negative integer`, 1);
    return 0;
  }
  return value;
}

function finiteNonNegative(value, name, nullable = false) {
  if (nullable && value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    fail("invalid_stats", `${name} must be a non-negative number`, 1);
    return 0;
  }
  return value;
}

function summarizeStats(data) {
  if (!data || typeof data !== "object" || Array.isArray(data) || data.sessionId !== sessionId) {
    fail("invalid_stats", "Pi stats response is invalid", 1);
    return null;
  }
  const tokens = data.tokens;
  if (!tokens || typeof tokens !== "object" || Array.isArray(tokens)) {
    fail("invalid_stats", "Pi stats tokens are invalid", 1);
    return null;
  }
  const summary = {
    sessionId,
    userMessages: nonNegativeInteger(data.userMessages, "userMessages"),
    assistantMessages: nonNegativeInteger(data.assistantMessages, "assistantMessages"),
    toolCalls: nonNegativeInteger(data.toolCalls, "toolCalls"),
    toolResults: nonNegativeInteger(data.toolResults, "toolResults"),
    totalMessages: nonNegativeInteger(data.totalMessages, "totalMessages"),
    tokens: {
      input: finiteNonNegative(tokens.input, "tokens.input"),
      output: finiteNonNegative(tokens.output, "tokens.output"),
      cacheRead: finiteNonNegative(tokens.cacheRead, "tokens.cacheRead"),
      cacheWrite: finiteNonNegative(tokens.cacheWrite, "tokens.cacheWrite"),
      total: finiteNonNegative(tokens.total, "tokens.total"),
    },
    cost: finiteNonNegative(data.cost, "cost"),
  };
  if (data.contextUsage && typeof data.contextUsage === "object") {
    summary.contextUsage = {
      tokens: finiteNonNegative(data.contextUsage.tokens, "contextUsage.tokens", true),
      contextWindow: finiteNonNegative(data.contextUsage.contextWindow, "contextUsage.contextWindow"),
      percent: finiteNonNegative(data.contextUsage.percent, "contextUsage.percent", true),
    };
  }
  return summary;
}

function maybeStart() {
  if (
    lifecycle !== "booting" ||
    !initial.modelSelected ||
    !initial.thinkingSelected ||
    !initial.state ||
    !initial.stats
  ) return;
  if (requireResume && initial.state.messageCount === 0) {
    fail(
      "session_resume_unavailable",
      "the required Pi session has no prior messages; refusing to execute a continuation prompt",
      1,
      true,
    );
    return;
  }
  lifecycle = "running";
  runStartedAt = Date.now();
  emit("started", {
    sessionId,
    model: modelRef,
    thinkingLevel,
    contextWindow,
    resumed: initial.state.messageCount > 0,
    initialStats: initial.stats,
  });
  startActivityTimer();
  send("prompt", "prompt");
}

function onSettled() {
  if (lifecycle !== "running" || !promptAccepted) {
    fail("invalid_lifecycle", "agent_settled arrived before prompt acceptance or while not running", 1);
    return;
  }
  if (activeToolCalls.size) {
    fail("invalid_tool_lifecycle", "Pi settled with unfinished tool calls", 1, true);
    return;
  }
  if (
    structuredOutputMode &&
    structuredOutputSuccesses !== 1 &&
    userInputSuccesses !== 1
  ) {
    fail(
      "missing_structured_output",
      `Pi must complete ${STRUCTURED_OUTPUT_TOOL} or ${USER_INPUT_TOOL} successfully before settling`,
      1,
      true,
    );
    return;
  }
  if (userInputSuccesses === 1 && inputRequest === null) {
    fail("invalid_user_input_request", "Pi completed user input without a safe request", 1, true);
    return;
  }
  lifecycle = "settled";
  send("final-state", "get_state");
  send("final-stats", "get_session_stats");
}

function maybeFinalize() {
  if (lifecycle !== "settled" || !final.state || !final.stats) return;
  if (delegatedChildTaskCount > 0) {
    for (const key of ["input", "output", "cacheRead", "cacheWrite"]) {
      const observed = final.stats.tokens[key] - initial.stats.tokens[key];
      if (observed < delegatedUsage[key]) {
        fail("invalid_delegation_usage", "parent session did not persist delegated token usage", 1, true);
        return;
      }
    }
    if (final.stats.cost - initial.stats.cost + 1e-9 < delegatedUsage.cost) {
      fail("invalid_delegation_usage", "parent session did not persist delegated cost usage", 1, true);
      return;
    }
  }
  lifecycle = "finalizing";
  if (timeoutHandle) clearTimeout(timeoutHandle);
  clearActivityTimer();
  child.stdin.end();
}

function maybeComplete() {
  if (lifecycle !== "finalizing" || !stdoutEnded || !childExited) return;
  if (childExitCode !== 0 || childExitSignal !== null || pendingResponses.size) {
    fail("invalid_exit", "Pi did not exit cleanly after final responses", 1);
    return;
  }
  lifecycle = "completed";
  emit("completed", {
    sessionId,
    state: final.state,
    stats: final.stats,
    inputRequest,
    telemetry: {
      firstToolElapsedMs,
      firstEditOrWriteToolElapsedMs,
      toolCounts: { ...toolCounts },
      lastStopReason,
      delegation: {
        requestedTasks: delegatedTaskCount,
        completedTasks: delegatedChildTaskCount,
        childTurns: delegatedChildTurns,
        childToolCalls: delegatedChildToolCalls,
      },
    },
  });
  cleanPrivateConfiguration();
  process.exit(0);
}

function publicTextBlocks(content) {
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block && typeof block === "object" && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("\n");
}

function sanitizePublic(value, limit = LIMITS.publicArgumentCharacters, depth = 0) {
  if (depth > 20) return null;
  if (typeof value === "string") return bounded(redact(value), limit);
  if (Array.isArray(value)) {
    return value.slice(0, LIMITS.arrayItems)
      .filter((item) => !(item && typeof item === "object" && ["thinking", "reasoning"].includes(item.type)))
      .map((item) => sanitizePublic(item, limit, depth + 1));
  }
  if (value && typeof value === "object") {
    if (["thinking", "reasoning"].includes(value.type)) return null;
    const output = {};
    for (const [key, item] of Object.entries(value)) {
      if (["thinking", "reasoning_content"].includes(key)) continue;
      output[key] = sanitizePublic(item, limit, depth + 1);
    }
    return output;
  }
  return value;
}

function structuredOutputArguments(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail("invalid_structured_output", `${STRUCTURED_OUTPUT_TOOL} arguments must be an object`, 1, true);
    return null;
  }
  let serialized;
  try {
    serialized = JSON.stringify(value);
  } catch {
    fail("invalid_structured_output", `${STRUCTURED_OUTPUT_TOOL} arguments must be JSON`, 1, true);
    return null;
  }
  if (!serialized) return null;
  try {
    // Unlike ordinary diagnostic tool arguments, this is the machine result;
    // preserve its complete structure while retaining the bridge's redaction.
    return JSON.parse(redact(serialized));
  } catch {
    fail("invalid_structured_output", `${STRUCTURED_OUTPUT_TOOL} arguments are invalid`, 1, true);
    return null;
  }
}

function normalizeUserInputArguments(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return { value: null, error: `${USER_INPUT_TOOL} arguments must be an object` };
  }
  const allowedKeys = new Set(["question", "choices", "allowFreeform", "reason"]);
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) {
    return { value: null, error: `${USER_INPUT_TOOL} arguments contain unknown fields` };
  }

  if (typeof value.question !== "string") {
    return { value: null, error: `${USER_INPUT_TOOL}.question must be a string` };
  }
  const question = value.question.trim();
  if (
    !question ||
    question.length > LIMITS.userInputQuestionCharacters ||
    /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/.test(question)
  ) {
    return { value: null, error: `${USER_INPUT_TOOL}.question is empty, oversized, or unsafe` };
  }

  if (typeof value.allowFreeform !== "boolean") {
    return { value: null, error: `${USER_INPUT_TOOL}.allowFreeform must be a boolean` };
  }
  const rawChoices = value.choices ?? [];
  if (!Array.isArray(rawChoices) || rawChoices.length > LIMITS.userInputChoices) {
    return { value: null, error: `${USER_INPUT_TOOL}.choices must be a bounded array` };
  }
  const choices = [];
  for (const rawChoice of rawChoices) {
    if (typeof rawChoice !== "string") {
      return { value: null, error: `${USER_INPUT_TOOL}.choices must contain only strings` };
    }
    const choice = rawChoice.trim();
    if (
      !choice ||
      choice.length > LIMITS.userInputChoiceCharacters ||
      /[\u0000-\u001F\u007F]/.test(choice)
    ) {
      return { value: null, error: `${USER_INPUT_TOOL}.choices contain an empty, oversized, or unsafe value` };
    }
    choices.push(choice);
  }
  if (new Set(choices).size !== choices.length) {
    return { value: null, error: `${USER_INPUT_TOOL}.choices must be unique` };
  }
  if (!value.allowFreeform && choices.length === 0) {
    return { value: null, error: `${USER_INPUT_TOOL} requires choices or free-form input` };
  }

  let reason;
  if (Object.hasOwn(value, "reason")) {
    if (typeof value.reason !== "string") {
      return { value: null, error: `${USER_INPUT_TOOL}.reason must be a string` };
    }
    reason = value.reason.trim();
    if (
      !reason ||
      reason.length > LIMITS.userInputReasonCharacters ||
      /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/.test(reason)
    ) {
      return { value: null, error: `${USER_INPUT_TOOL}.reason is empty, oversized, or unsafe` };
    }
  }

  const normalized = {
    question: redact(question),
    choices: choices.map((choice) => redact(choice)),
    allowFreeform: value.allowFreeform,
  };
  if (reason !== undefined) normalized.reason = redact(reason);
  return { value: normalized, error: null };
}

function normalizeDelegateArguments(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).some((key) => key !== "tasks") ||
    !Array.isArray(value.tasks) ||
    value.tasks.length < 1 ||
    value.tasks.length > LIMITS.delegatedTasks ||
    delegatedTaskCount + value.tasks.length > LIMITS.delegatedTasks
  ) {
    return { value: null, publicValue: null };
  }
  const taskIds = [];
  const seen = new Set();
  for (const task of value.tasks) {
    if (
      !task ||
      typeof task !== "object" ||
      Array.isArray(task) ||
      Object.keys(task).some((key) => key !== "id" && key !== "task") ||
      typeof task.id !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/.test(task.id) ||
      task.id.length > LIMITS.delegatedTaskIdCharacters ||
      seen.has(task.id) ||
      typeof task.task !== "string" ||
      !task.task.trim() ||
      task.task.length > LIMITS.delegatedTaskCharacters ||
      /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/.test(task.task)
    ) {
      return { value: null, publicValue: null };
    }
    seen.add(task.id);
    taskIds.push(task.id);
  }
  return {
    value: taskIds,
    // Task bodies may contain repository excerpts. Public progress exposes
    // only caller-chosen bounded identifiers, never child output or prompts.
    publicValue: { tasks: taskIds.map((id) => ({ id })) },
  };
}

function delegationInteger(value) {
  if (!Number.isSafeInteger(value) || value < 0 || value > 1_000_000_000) {
    throw new Error("invalid delegated integer usage");
  }
  return value;
}

function delegationCost(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error("invalid delegated cost usage");
  }
  return value;
}

function normalizeDelegatedUsage(value, { child = false } = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("delegated usage must be an object");
  }
  const expectedKeys = new Set([
    "input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost",
    ...(child ? ["toolCalls", "turns"] : []),
  ]);
  if (Object.keys(value).some((key) => !expectedKeys.has(key))) {
    throw new Error("delegated usage contains unknown fields");
  }
  if (!value.cost || typeof value.cost !== "object" || Array.isArray(value.cost)) {
    throw new Error("delegated cost must be an object");
  }
  const costKeys = new Set(["input", "output", "cacheRead", "cacheWrite", "total"]);
  if (Object.keys(value.cost).some((key) => !costKeys.has(key))) {
    throw new Error("delegated cost contains unknown fields");
  }
  const usage = {
    input: delegationInteger(value.input),
    output: delegationInteger(value.output),
    cacheRead: delegationInteger(value.cacheRead),
    cacheWrite: delegationInteger(value.cacheWrite),
    totalTokens: delegationInteger(value.totalTokens),
    cost: {
      input: delegationCost(value.cost.input),
      output: delegationCost(value.cost.output),
      cacheRead: delegationCost(value.cost.cacheRead),
      cacheWrite: delegationCost(value.cost.cacheWrite),
      total: delegationCost(value.cost.total),
    },
    toolCalls: child ? delegationInteger(value.toolCalls) : 0,
    turns: child ? delegationInteger(value.turns) : 0,
  };
  return usage;
}

function approximatelyEqual(left, right) {
  return Math.abs(left - right) <= 1e-9 * Math.max(1, Math.abs(left), Math.abs(right));
}

function normalizeDelegationResult(result, expectedTaskIds) {
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    throw new Error("delegation result must be an object");
  }
  const details = result.details;
  if (
    !details ||
    typeof details !== "object" ||
    Array.isArray(details) ||
    Object.keys(details).some((key) => !["schemaVersion", "kind", "results"].includes(key)) ||
    details.schemaVersion !== 1 ||
    details.kind !== "fomo.delegate_subtasks.result" ||
    !Array.isArray(details.results) ||
    details.results.length !== expectedTaskIds.length
  ) {
    throw new Error("delegation details are invalid");
  }

  const aggregate = {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
    toolCalls: 0,
    turns: 0,
  };
  details.results.forEach((item, index) => {
    if (
      !item ||
      typeof item !== "object" ||
      Array.isArray(item) ||
      Object.keys(item).some((key) => !["id", "status", "usage"].includes(key)) ||
      item.id !== expectedTaskIds[index] ||
      !["succeeded", "failed", "cancelled"].includes(item.status)
    ) {
      throw new Error("delegation child result is invalid");
    }
    const usage = normalizeDelegatedUsage(item.usage, { child: true });
    if (usage.totalTokens !== usage.input + usage.output + usage.cacheRead + usage.cacheWrite) {
      throw new Error("delegation child total token usage is inconsistent");
    }
    for (const key of ["input", "output", "cacheRead", "cacheWrite", "totalTokens", "toolCalls", "turns"]) {
      aggregate[key] += usage[key];
      if (!Number.isSafeInteger(aggregate[key])) throw new Error("delegated usage overflow");
    }
    for (const key of ["input", "output", "cacheRead", "cacheWrite", "total"]) {
      aggregate.cost[key] += usage.cost[key];
      if (!Number.isFinite(aggregate.cost[key])) throw new Error("delegated cost overflow");
    }
  });

  const parentUsage = normalizeDelegatedUsage(result.usage);
  for (const key of ["input", "output", "cacheRead", "cacheWrite"]) {
    if (parentUsage[key] !== aggregate[key]) {
      throw new Error("delegation parent usage does not match child usage");
    }
  }
  const componentTotal = aggregate.input + aggregate.output + aggregate.cacheRead + aggregate.cacheWrite;
  if (parentUsage.totalTokens !== componentTotal) {
    throw new Error("delegation total token usage is inconsistent");
  }
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "total"]) {
    if (!approximatelyEqual(parentUsage.cost[key], aggregate.cost[key])) {
      throw new Error("delegation parent cost does not match child usage");
    }
  }
  return { usage: parentUsage, toolCalls: aggregate.toolCalls, turns: aggregate.turns };
}

function emitPi(kind, payload = {}) {
  if (!PUBLIC_PI_KINDS.has(kind)) {
    fail("unknown_public_event", `bridge attempted unknown public Pi event ${kind}`, 1);
    return;
  }
  emit("pi.event", { kind, ...payload });
}

function touchAgentActivity() {
  lastAgentActivityAt = Date.now();
}

function beginToolCall(message) {
  touchAgentActivity();
  const toolName = String(message.toolName ?? "");
  const toolCallId = String(message.toolCallId ?? "");
  if (
    !toolCallId ||
    activeToolCalls.has(toolCallId) ||
    completedToolCallIds.has(toolCallId) ||
    !allowedToolNames.has(toolName)
  ) {
    fail("invalid_tool_lifecycle", "Pi emitted an invalid or duplicate tool start", 1, true);
    return false;
  }
  if (structuredOutputSuccesses !== 0) {
    fail("invalid_structured_output", `Pi must stop after ${STRUCTURED_OUTPUT_TOOL} succeeds`, 1, true);
    return false;
  }
  if (userInputSuccesses !== 0) {
    fail("invalid_user_input_request", `Pi must stop after ${USER_INPUT_TOOL} succeeds`, 1, true);
    return false;
  }
  if (userInputArgumentsByCallId.size !== 0) {
    fail("invalid_user_input_request", `${USER_INPUT_TOOL} must be the only active tool`, 1, true);
    return false;
  }
  if (delegatedTaskIdsByCallId.size !== 0) {
    fail(
      "invalid_delegation",
      `${DELEGATE_SUBTASKS_TOOL} must be the only active parent tool`,
      1,
      true,
    );
    return false;
  }
  if (structuredOutputMode && toolName !== STRUCTURED_OUTPUT_TOOL && toolName !== USER_INPUT_TOOL) {
    fail(
      "invalid_structured_output",
      `Pi may only call ${STRUCTURED_OUTPUT_TOOL} or ${USER_INPUT_TOOL}`,
      1,
      true,
    );
    return false;
  }
  if (toolName === STRUCTURED_OUTPUT_TOOL) {
    if (!structuredOutputMode) {
      fail("invalid_structured_output", `${STRUCTURED_OUTPUT_TOOL} is unavailable`, 1, true);
      return false;
    }
    if (activeToolCalls.size !== 0) {
      fail("invalid_structured_output", `${STRUCTURED_OUTPUT_TOOL} must run serially`, 1, true);
      return false;
    }
  }
  if (toolName === USER_INPUT_TOOL) {
    if (!userInputEnabled) {
      fail("invalid_user_input_request", `${USER_INPUT_TOOL} is unavailable`, 1, true);
      return false;
    }
    if (activeToolCalls.size !== 0 || userInputCalls >= MAX_USER_INPUT_ATTEMPTS) {
      fail(
        "invalid_user_input_request",
        `Pi may attempt ${USER_INPUT_TOOL} at most ${MAX_USER_INPUT_ATTEMPTS} times`,
        1,
        true,
      );
      return false;
    }
    userInputCalls += 1;
    userInputArgumentsByCallId.set(toolCallId, normalizeUserInputArguments(message.args));
  }
  if (toolName === DELEGATE_SUBTASKS_TOOL) {
    if (!allowedToolNames.has(DELEGATE_SUBTASKS_TOOL) || activeToolCalls.size !== 0) {
      fail("invalid_delegation", `${DELEGATE_SUBTASKS_TOOL} is unavailable or concurrent`, 1, true);
      return false;
    }
    const normalized = normalizeDelegateArguments(message.args);
    if (normalized.value === null) {
      fail("invalid_delegation", `${DELEGATE_SUBTASKS_TOOL} arguments are invalid`, 1, true);
      return false;
    }
    delegatedTaskCount += normalized.value.length;
    delegatedTaskIdsByCallId.set(toolCallId, {
      ids: normalized.value,
      publicValue: normalized.publicValue,
    });
  }
  activeToolCalls.set(toolCallId, toolName);
  if (firstToolElapsedMs === null && runStartedAt > 0) {
    firstToolElapsedMs = Date.now() - runStartedAt;
  }
  toolCounts[toolName] = (toolCounts[toolName] ?? 0) + 1;
  return true;
}

function endToolCall(message, ending) {
  touchAgentActivity();
  const toolCallId = String(message.toolCallId ?? "");
  const toolName = String(message.toolName ?? "");
  if (!toolCallId || activeToolCalls.get(toolCallId) !== toolName) {
    fail("invalid_tool_lifecycle", "Pi emitted unmatched tool progress", 1, true);
    return false;
  }
  if (ending) {
    if (typeof message.isError !== "boolean") {
      fail("invalid_tool_lifecycle", "Pi emitted a tool end without a boolean result", 1, true);
      return false;
    }
    activeToolCalls.delete(toolCallId);
    completedToolCallIds.add(toolCallId);
    if (structuredOutputMode && toolName === STRUCTURED_OUTPUT_TOOL) {
      if (message.isError !== true) {
        structuredOutputSuccesses += 1;
      }
    }
    if (toolName === USER_INPUT_TOOL) {
      const normalized = userInputArgumentsByCallId.get(toolCallId);
      userInputArgumentsByCallId.delete(toolCallId);
      if (message.isError !== true) {
        if (!normalized || normalized.value === null) {
          fail(
            "invalid_user_input_request",
            normalized?.error || `${USER_INPUT_TOOL} arguments are unavailable`,
            1,
            true,
          );
          return false;
        }
        userInputSuccesses += 1;
        inputRequest = {
          requestId: `input-${randomUUID()}`,
          ...normalized.value,
        };
      }
    }
    if (toolName === DELEGATE_SUBTASKS_TOOL) {
      const expected = delegatedTaskIdsByCallId.get(toolCallId);
      delegatedTaskIdsByCallId.delete(toolCallId);
      if (!expected) {
        fail("invalid_delegation", "delegation result has no matching task contract", 1, true);
        return false;
      }
      if (message.isError !== true) {
        try {
          const normalized = normalizeDelegationResult(message.result, expected.ids);
          delegatedChildTaskCount += expected.ids.length;
          delegatedChildToolCalls += normalized.toolCalls;
          delegatedChildTurns += normalized.turns;
          for (const key of ["input", "output", "cacheRead", "cacheWrite"]) {
            delegatedUsage[key] += normalized.usage[key];
          }
          delegatedUsage.cost += normalized.usage.cost.total;
        } catch {
          fail("invalid_delegation", "delegation result or usage is invalid", 1, true);
          return false;
        }
      }
    }
    if (
      (toolName === "edit" || toolName === "write") &&
      message.isError !== true &&
      firstEditOrWriteToolElapsedMs === null &&
      runStartedAt > 0
    ) {
      firstEditOrWriteToolElapsedMs = Date.now() - runStartedAt;
    }
  }
  return true;
}

function startActivityTimer() {
  // Protocol silence is normal while a provider is thinking. Emit liveness
  // heartbeats indefinitely; only process exit/EOF, transport failure,
  // cancellation, lease loss, sandbox expiry, or spend policy may terminate.
  const cadenceMs = (activitySilenceSeconds ?? 30) * 1000;
  const heartbeatIntervalMs = Math.min(15_000, Math.max(250, cadenceMs / 2));
  const pollIntervalMs = Math.min(1_000, Math.max(100, heartbeatIntervalMs / 2));
  lastAgentActivityAt = Date.now();
  lastInferenceHeartbeatAt = lastAgentActivityAt;
  activityTimerHandle = setInterval(() => {
    if (lifecycle !== "running") return;
    const now = Date.now();
    if (now - lastInferenceHeartbeatAt >= heartbeatIntervalMs) {
      // A heartbeat only proves that the bridge remains alive and is still
      // awaiting Pi. It never exposes private reasoning or resets the watchdog.
      emitPi("inference_heartbeat");
      lastInferenceHeartbeatAt = now;
    }
  }, pollIntervalMs);
}

function clearActivityTimer() {
  if (!activityTimerHandle) return;
  clearInterval(activityTimerHandle);
  activityTimerHandle = null;
}

function handleEvent(message) {
  if (lifecycle !== "running") {
    fail("invalid_lifecycle", `Pi event ${message.type} arrived while ${lifecycle}`, 1);
    return;
  }
  // This includes private thinking deltas: their content remains suppressed,
  // but receipt of a valid Pi event proves a high-thinking turn is not stuck.
  touchAgentActivity();
  switch (message.type) {
    case "agent_start": emitPi("agent_start"); break;
    case "agent_end": emitPi("agent_end", { willRetry: message.willRetry === true }); break;
    case "agent_settled": emitPi("agent_settled"); onSettled(); break;
    case "turn_start": emitPi("turn_start"); break;
    case "turn_end": {
      const stopReason = String(message.message?.stopReason ?? "");
      if (message.message?.role === "assistant" && stopReason) lastStopReason = stopReason;
      emitPi("turn_end", {
        role: String(message.message?.role ?? ""),
        stopReason,
        text: bounded(redact(publicTextBlocks(message.message?.content)), LIMITS.publicTextCharacters),
        toolResults: Array.isArray(message.toolResults) ? message.toolResults.slice(0, LIMITS.arrayItems).map((result) => ({
          toolCallId: String(result?.toolCallId ?? ""),
          toolName: String(result?.toolName ?? ""),
          isError: result?.isError === true,
        })) : [],
      });
      break;
    }
    case "message_start": emitPi("message_start", { role: String(message.message?.role ?? "") }); break;
    case "message_end": {
      const stopReason = String(message.message?.stopReason ?? "");
      if (message.message?.role === "assistant" && stopReason) lastStopReason = stopReason;
      emitPi("message_end", {
        role: String(message.message?.role ?? ""),
        stopReason,
      });
      break;
    }
    case "message_update": handleMessageDelta(message.assistantMessageEvent); break;
    case "bash_execution_update": emitPi("bash_output", {
      delta: bounded(redact(message.delta), LIMITS.publicDeltaCharacters),
    }); break;
    case "tool_execution_start":
      if (beginToolCall(message)) {
        const toolName = String(message.toolName ?? "");
        let args;
        if (toolName === STRUCTURED_OUTPUT_TOOL) {
          args = structuredOutputArguments(message.args);
        } else if (toolName === USER_INPUT_TOOL) {
          args = userInputArgumentsByCallId.get(String(message.toolCallId ?? ""))?.value ?? {};
        } else if (toolName === DELEGATE_SUBTASKS_TOOL) {
          args = delegatedTaskIdsByCallId.get(String(message.toolCallId ?? ""))?.publicValue ?? {};
        } else {
          args = sanitizePublic(message.args);
        }
        if (args !== null && lifecycle === "running") emitPi("tool_start", {
          toolCallId: String(message.toolCallId ?? ""),
          toolName,
          args,
          elapsedMs: runStartedAt > 0 ? Date.now() - runStartedAt : null,
        });
      }
      break;
    case "tool_execution_update":
      if (endToolCall(message, false)) emitPi("tool_output", {
        toolCallId: String(message.toolCallId ?? ""),
        toolName: String(message.toolName ?? ""),
        text: bounded(redact(publicTextBlocks(message.partialResult?.content)), LIMITS.publicTextCharacters),
        cumulative: true,
        elapsedMs: runStartedAt > 0 ? Date.now() - runStartedAt : null,
      });
      break;
    case "tool_execution_end":
      if (endToolCall(message, true)) {
        emitPi("tool_end", {
          toolCallId: String(message.toolCallId ?? ""),
          toolName: String(message.toolName ?? ""),
          isError: message.isError === true,
          elapsedMs: runStartedAt > 0 ? Date.now() - runStartedAt : null,
        });
        if (message.toolName === USER_INPUT_TOOL && message.isError === false && inputRequest) {
          emitPi("input_request", { inputRequest });
        }
      }
      break;
    case "queue_update": emitPi("queue_update", {
      steering: sanitizePublic(message.steering, LIMITS.publicDeltaCharacters),
      followUp: sanitizePublic(message.followUp, LIMITS.publicDeltaCharacters),
    }); break;
    case "compaction_start": emitPi("compaction_start", { reason: String(message.reason ?? "") }); break;
    case "compaction_end": emitPi("compaction_end", {
      reason: String(message.reason ?? ""), aborted: message.aborted === true, willRetry: message.willRetry === true,
    }); break;
    case "auto_retry_start": emitPi("auto_retry_start", {
      attempt: Number(message.attempt ?? 0), maxAttempts: Number(message.maxAttempts ?? 0),
    }); break;
    case "auto_retry_end": emitPi("auto_retry_end", {
      success: message.success === true, attempt: Number(message.attempt ?? 0),
    }); break;
    case "summarization_retry_scheduled": emitPi("summarization_retry_scheduled", {
      attempt: Number(message.attempt ?? 0), maxAttempts: Number(message.maxAttempts ?? 0),
    }); break;
    case "summarization_retry_attempt_start": emitPi("summarization_retry_attempt_start", {
      source: String(message.source ?? ""),
    }); break;
    case "summarization_retry_finished": emitPi("summarization_retry_finished"); break;
    case "extension_error": emitPi("extension_error", {
      extensionPath: bounded(redact(message.extensionPath), 512), event: String(message.event ?? ""),
    }); break;
    default: fail("unknown_pi_event", `unknown Pi event ${message.type}`, 1);
  }
}

function handleMessageDelta(delta) {
  if (!delta || typeof delta !== "object" || typeof delta.type !== "string") {
    fail("invalid_message_delta", "message_update is missing assistantMessageEvent", 1);
    return;
  }
  if (delta.type.startsWith("thinking_") || delta.type.startsWith("toolcall_")) return;
  if (delta.type === "text_start" || delta.type === "text_end") {
    emitPi("message_delta", { deltaType: delta.type, contentIndex: Number(delta.contentIndex ?? 0) });
  } else if (delta.type === "text_delta") {
    emitPi("message_delta", {
      deltaType: delta.type,
      contentIndex: Number(delta.contentIndex ?? 0),
      delta: bounded(redact(delta.delta), LIMITS.publicDeltaCharacters),
    });
  } else {
    fail("unknown_message_delta", `unknown assistant delta ${delta.type}`, 1);
  }
}

function killProcessGroup(signal) {
  if (!child?.pid) return;
  try { process.kill(-child.pid, signal); } catch (error) {
    if (error.code !== "ESRCH") diagnostic(`cannot signal Pi process group: ${error.message}`);
  }
}

async function shutdown(exitCode, immediate = false) {
  if (shuttingDown) return;
  shuttingDown = true;
  if (timeoutHandle) clearTimeout(timeoutHandle);
  clearActivityTimer();
  if (child && !childExited) {
    try { child.stdin.write(`${JSON.stringify({ id: "abort", type: "abort" })}\n`); } catch { /* continue */ }
    if (immediate) killProcessGroup("SIGTERM");
    await new Promise((resolve) => setTimeout(resolve, immediate ? 1000 : graceSeconds * 1000));
    if (!childExited) {
      killProcessGroup("SIGTERM");
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (!childExited) killProcessGroup("SIGKILL");
    }
  }
  cleanPrivateConfiguration();
  process.exit(exitCode);
}

function fail(code, message, exitCode, immediate = false) {
  if (lifecycle === "failed" || lifecycle === "completed") return;
  const phase = lifecycle;
  lifecycle = "failed";
  emit("failed", { code, message: bounded(redact(message), 4096), phase });
  void shutdown(exitCode, immediate);
}

function main() {
  try {
    parseEnvironment();
    const sessionsDir = writePrivateConfiguration();
    lifecycle = "booting";
    spawnPi(sessionsDir);
    send("select-model", "set_model", { provider: PROVIDER_ID, modelId: modelConfig.id });
    send("select-thinking", "set_thinking_level", { level: effectiveThinkingLevel });
    send("initial-state", "get_state");
    send("initial-stats", "get_session_stats");
    if (timeoutSeconds !== null) {
      timeoutHandle = setTimeout(() => fail("timeout", `bridge exceeded ${timeoutSeconds} seconds`, 124), timeoutSeconds * 1000);
    }
  } catch (error) {
    fail("invalid_environment", error instanceof Error ? error.message : "bridge setup failed", 1);
  }
}

process.on("SIGTERM", () => fail("terminated", "bridge received SIGTERM", 143));
process.on("SIGINT", () => fail("terminated", "bridge received SIGINT", 130));
process.on("uncaughtException", (error) => fail("bridge_error", error.message, 1));
process.on("unhandledRejection", (reason) => fail("bridge_error", String(reason), 1));

main();
