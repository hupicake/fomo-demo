#!/usr/bin/env node
/**
 * Trusted foreground bridge between OpenSandbox and OpenCode.
 *
 * OpenCode runs as a loopback-only server in generation sandbox G. The bridge
 * owns its lifecycle, stores only an opaque FOMO-to-OpenCode session mapping,
 * and translates OpenCode's SDK v2 stream into the existing strict FOMO JSONL
 * contract. stdout is protocol-only; prompts, keys, provider responses, and
 * private reasoning never enter diagnostics or lifecycle payloads.
 */

import { randomUUID } from "node:crypto";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { TextDecoder } from "node:util";

const SCHEMA_VERSION = 1;
const PROVIDER_ID = "fomo-litellm";
const STRUCTURED_OUTPUT_TOOL = "submit_structured_output";
const DEFAULT_SDK_PATH = "/opt/fomo/pi/lib/node_modules/@opencode-ai/sdk/dist/v2/index.js";
const SESSION_MAPPING_SCHEMA_VERSION = 2;
const SESSION_POLICY_VERSION = 1;

const SESSION_STAGE = Object.freeze({
  planner: "planner",
  workspace: "workspace",
});

const SESSION_PERMISSION_PROFILES = Object.freeze({
  [SESSION_STAGE.planner]: Object.freeze([
    permissionRule("read", "deny"),
    permissionRule("edit", "deny"),
    permissionRule("glob", "deny"),
    permissionRule("grep", "deny"),
    permissionRule("list", "deny"),
    permissionRule("bash", "deny"),
    permissionRule("todowrite", "deny"),
    permissionRule("task", "deny"),
    permissionRule("question", "deny"),
    permissionRule("skill", "deny"),
    permissionRule("webfetch", "deny"),
    permissionRule("websearch", "deny"),
    permissionRule("external_directory", "deny"),
    permissionRule("doom_loop", "deny"),
  ]),
  [SESSION_STAGE.workspace]: Object.freeze([
    permissionRule("read", "allow"),
    // OpenCode resolves edit, write, and GPT's apply_patch through the
    // semantic `edit` permission.
    permissionRule("edit", "allow"),
    permissionRule("glob", "allow"),
    permissionRule("grep", "allow"),
    permissionRule("list", "allow"),
    permissionRule("bash", "allow"),
    permissionRule("todowrite", "allow"),
    permissionRule("task", "deny"),
    permissionRule("question", "deny"),
    permissionRule("skill", "deny"),
    permissionRule("webfetch", "deny"),
    permissionRule("websearch", "deny"),
    permissionRule("external_directory", "deny"),
    permissionRule("doom_loop", "deny"),
  ]),
});

function permissionRule(permission, action) {
  return Object.freeze({ permission, pattern: "*", action });
}

const MODEL_CONFIGS = Object.freeze({
  [`${PROVIDER_ID}/fomo-pi-flash`]: modelConfig(
    "fomo-pi-flash", ["off", "high", "max"], 1_000_000, 384_000,
  ),
  [`${PROVIDER_ID}/fomo-pi-build`]: modelConfig(
    "fomo-pi-build", ["off", "medium", "high"], 250_000, 128_000,
  ),
  [`${PROVIDER_ID}/fomo-pi-gpt-5.6`]: modelConfig(
    "fomo-pi-gpt-5.6", ["off", "low", "medium", "high", "xhigh", "max"], 250_000, 128_000,
  ),
  [`${PROVIDER_ID}/fomo-pi-gpt-5.5`]: modelConfig(
    "fomo-pi-gpt-5.5", ["off", "low", "medium", "high", "xhigh"], 250_000, 128_000,
  ),
  [`${PROVIDER_ID}/fomo-pi-deepseek-flash`]: modelConfig(
    "fomo-pi-deepseek-flash", ["off", "high"], 1_000_000, 384_000,
  ),
  [`${PROVIDER_ID}/fomo-pi-grok-4.5`]: modelConfig(
    "fomo-pi-grok-4.5", ["low", "medium", "high"], 500_000, 500_000,
  ),
  [`${PROVIDER_ID}/fomo-pi-kimi-k2.7-code`]: modelConfig(
    "fomo-pi-kimi-k2.7-code", ["default"], 262_144, 262_144,
  ),
  [`${PROVIDER_ID}/fomo-pi-gemini-3.6-flash`]: modelConfig(
    "fomo-pi-gemini-3.6-flash", ["minimal", "low", "medium", "high"], 250_000, 65_536,
  ),
  [`${PROVIDER_ID}/fomo-pi-gemini-3.1-pro`]: modelConfig(
    "fomo-pi-gemini-3.1-pro", ["low", "medium", "high"], 250_000, 65_536,
  ),
});

function modelConfig(id, thinkingLevels, maxContextWindow, maxOutputTokens) {
  return Object.freeze({ id, thinkingLevels: Object.freeze(thinkingLevels), maxContextWindow, maxOutputTokens });
}

const ENV = Object.freeze({
  prompt: "FOMO_PI_PROMPT_B64",
  sessionId: "FOMO_PI_SESSION_ID",
  requestId: "FOMO_PI_REQUEST_ID",
  correlationId: "FOMO_PI_CORRELATION_ID",
  providerBaseUrl: "FOMO_PI_PROVIDER_BASE_URL",
  virtualKey: "FOMO_PI_VIRTUAL_KEY",
  workspace: "FOMO_PI_WORKSPACE",
  stateDir: "FOMO_PI_STATE_DIR",
  openCodeBin: "FOMO_PI_BIN",
  thinkingLevel: "FOMO_PI_THINKING_LEVEL",
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
  stateDir: "/var/lib/fomo-opencode",
  openCodeBin: "/opt/fomo/pi/bin/opencode",
  modelRef: `${PROVIDER_ID}/fomo-pi-build`,
  thinkingLevel: "high",
  contextWindow: 200_000,
  graceSeconds: 10,
});

const BRIDGE_FAILURE_MESSAGES = Object.freeze({
  opencode_model_failed: "OpenCode model request failed.",
  opencode_runtime_failed: "OpenCode runtime could not complete the request.",
  opencode_capability_unavailable: "OpenCode runtime does not expose the capabilities required for this turn.",
  opencode_failed: "OpenCode failed unexpectedly.",
  bridge_error: "OpenCode bridge failed unexpectedly.",
  timeout: "OpenCode bridge exceeded its run time limit.",
  terminated: "OpenCode bridge was terminated.",
});

const MODEL_ERROR_NAMES = new Set([
  "ProviderAuthError",
  "UnknownError",
  "MessageOutputLengthError",
  "MessageAbortedError",
  "StructuredOutputError",
  "ContextOverflowError",
  "ContentFilterError",
  "APIError",
]);

class OpenCodeModelFailure extends Error {
  constructor(cause) {
    super(BRIDGE_FAILURE_MESSAGES.opencode_model_failed);
    this.name = "OpenCodeModelFailure";
    this.cause = cause;
  }
}

class OpenCodeRuntimeFailure extends Error {
  constructor(cause) {
    super(BRIDGE_FAILURE_MESSAGES.opencode_runtime_failed);
    this.name = "OpenCodeRuntimeFailure";
    this.cause = cause;
  }
}

class OpenCodeCapabilityFailure extends Error {
  constructor(cause) {
    super(BRIDGE_FAILURE_MESSAGES.opencode_capability_unavailable);
    this.name = "OpenCodeCapabilityFailure";
    this.cause = cause;
  }
}

const LIMITS = Object.freeze({
  promptCharacters: 100_000,
  identifierCharacters: 128,
  keyCharacters: 4096,
  publicTextCharacters: 8192,
  publicDeltaCharacters: 4096,
  publicArgumentCharacters: 2048,
  structuredOutputSchemaBytes: 64 * 1024,
  stderrBytes: 64 * 1024,
  arrayItems: 128,
});

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
const SESSION_ID = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/;
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;
const fatalUtf8 = new TextDecoder("utf-8", { fatal: true });

let requestId = "invalid-request";
let correlationId = "invalid-correlation";
let sessionId = "";
let prompt = "";
let promptBase64 = "";
let virtualKey = "";
let providerBaseUrl = "";
let workspace = DEFAULTS.workspace;
let stateDir = DEFAULTS.stateDir;
let openCodeBin = DEFAULTS.openCodeBin;
let modelRef = DEFAULTS.modelRef;
let model = MODEL_CONFIGS[DEFAULTS.modelRef];
let thinkingLevel = DEFAULTS.thinkingLevel;
let contextWindow = DEFAULTS.contextWindow;
let activitySilenceSeconds = null;
let timeoutSeconds = null;
let graceSeconds = DEFAULTS.graceSeconds;
let requireResume = false;
let structuredOutputSchemaBase64 = "";
let structuredOutputSchema = null;

let sequence = 0;
let lifecycle = "booting";
let stderrBytes = 0;
let runStartedAt = 0;
let timeoutHandle = null;
let heartbeatHandle = null;
let sdkServer = null;
let sdkClient = null;
let sdkSessionId = null;
let sseAbortController = null;
let shuttingDown = false;
let settled = false;
let eventCount = 0;
let firstToolElapsedMs = null;
let firstEditOrWriteToolElapsedMs = null;
let lastStopReason = "";
const toolCounts = {};
const activeTools = new Map();
const completedTools = new Set();
const startedTurns = new Set();
const endedTurns = new Set();
const startedTexts = new Set();
const completedTexts = new Set();
let sawPublicTextDelta = false;
let sawTurn = false;
let terminalError = null;
let privateRuntimeDir = null;
let workspaceInvocation = null;

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

function emitPi(kind, payload = {}) {
  emit("pi.event", { kind, ...payload });
}

function diagnostic(message) {
  if (stderrBytes >= LIMITS.stderrBytes) return;
  const rendered = `[fomo-opencode-bridge] ${bounded(redact(message), 2048)}\n`;
  const bytes = Buffer.from(rendered);
  const visible = bytes.subarray(0, Math.max(0, LIMITS.stderrBytes - stderrBytes));
  stderrBytes += visible.length;
  if (visible.length) process.stderr.write(visible);
}

function required(name) {
  const value = process.env[name];
  if (typeof value !== "string" || value === "") throw new Error(`missing ${name}`);
  return value;
}

function positiveInteger(name, raw, maximum = Number.MAX_SAFE_INTEGER) {
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1 || value > maximum) {
    throw new Error(`${name} must be an integer between 1 and ${maximum}`);
  }
  return value;
}

function absolutePath(name, value) {
  if (typeof value !== "string" || !value.startsWith("/") || value.includes("\0")) {
    throw new Error(`${name} must be an absolute path`);
  }
  return value;
}

function decodeCanonicalBase64(name, value, maximumBytes) {
  if (!value || value.length % 4 !== 0 || !BASE64.test(value)) {
    throw new Error(`${name} must be canonical base64`);
  }
  const bytes = Buffer.from(value, "base64");
  if (!bytes.length || bytes.length > maximumBytes || bytes.toString("base64") !== value) {
    throw new Error(`${name} must decode within its byte limit`);
  }
  try {
    return fatalUtf8.decode(bytes);
  } catch {
    throw new Error(`${name} must contain UTF-8`);
  }
}

function featureFlag(name) {
  const value = process.env[name];
  if (value === undefined || value === "") return false;
  if (value !== "1") throw new Error(`${name} must be 1 when enabled`);
  return true;
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
  prompt = decodeCanonicalBase64(ENV.prompt, promptBase64, Buffer.byteLength("x".repeat(LIMITS.promptCharacters * 4)));
  if (!prompt.trim() || prompt.length > LIMITS.promptCharacters) {
    throw new Error("prompt must be non-empty and within the character limit");
  }

  virtualKey = required(ENV.virtualKey);
  if (virtualKey.length > LIMITS.keyCharacters || /[\x00-\x1f\x7f]/.test(virtualKey)) {
    throw new Error(`${ENV.virtualKey} is invalid`);
  }

  let url;
  try {
    url = new URL(required(ENV.providerBaseUrl));
  } catch {
    throw new Error(`${ENV.providerBaseUrl} is not a valid URL`);
  }
  if (
    !["http:", "https:"].includes(url.protocol) || url.username || url.password ||
    url.search || url.hash || !url.hostname || !url.pathname.replace(/\/+$/, "").endsWith("/v1")
  ) {
    throw new Error(`${ENV.providerBaseUrl} must be an http(s) URL ending in /v1 without userinfo, query, or fragment`);
  }
  url.pathname = url.pathname.replace(/\/+$/, "");
  providerBaseUrl = url.toString().replace(/\/$/, "");

  workspace = absolutePath(ENV.workspace, process.env[ENV.workspace] || DEFAULTS.workspace);
  stateDir = absolutePath(ENV.stateDir, process.env[ENV.stateDir] || DEFAULTS.stateDir);
  openCodeBin = absolutePath(ENV.openCodeBin, process.env[ENV.openCodeBin] || DEFAULTS.openCodeBin);
  if (!existsSync(workspace) || !statSync(workspace).isDirectory()) throw new Error(`${ENV.workspace} is not a directory`);
  if (!existsSync(openCodeBin) || !statSync(openCodeBin).isFile()) throw new Error(`${ENV.openCodeBin} is not a file`);

  modelRef = process.env[ENV.modelRef] || DEFAULTS.modelRef;
  model = MODEL_CONFIGS[modelRef];
  if (!model) throw new Error(`${ENV.modelRef} must select a supported FOMO model`);
  thinkingLevel = process.env[ENV.thinkingLevel] || DEFAULTS.thinkingLevel;
  if (!model.thinkingLevels.includes(thinkingLevel)) {
    throw new Error(`${ENV.thinkingLevel} is unsupported by ${modelRef}`);
  }
  contextWindow = process.env[ENV.contextWindow]
    ? positiveInteger(ENV.contextWindow, process.env[ENV.contextWindow], model.maxContextWindow)
    : DEFAULTS.contextWindow;
  if (contextWindow > model.maxContextWindow) throw new Error(`${ENV.contextWindow} exceeds the selected model limit`);
  if (process.env[ENV.activitySilenceSeconds]) {
    activitySilenceSeconds = positiveInteger(ENV.activitySilenceSeconds, process.env[ENV.activitySilenceSeconds], 3600);
  }
  if (process.env[ENV.timeoutSeconds]) timeoutSeconds = positiveInteger(ENV.timeoutSeconds, process.env[ENV.timeoutSeconds]);
  if (process.env[ENV.graceSeconds]) graceSeconds = positiveInteger(ENV.graceSeconds, process.env[ENV.graceSeconds], 60);

  requireResume = featureFlag(ENV.requireResume);
  if (featureFlag(ENV.userInputEnabled)) {
    throw new Error("OpenCode runtime does not yet support FOMO user-input continuations");
  }

  structuredOutputSchemaBase64 = process.env[ENV.structuredOutputSchema] || "";
  if (structuredOutputSchemaBase64) {
    const schemaText = decodeCanonicalBase64(
      ENV.structuredOutputSchema,
      structuredOutputSchemaBase64,
      LIMITS.structuredOutputSchemaBytes,
    );
    try {
      structuredOutputSchema = JSON.parse(schemaText);
    } catch {
      throw new Error(`${ENV.structuredOutputSchema} must contain valid JSON`);
    }
    if (!structuredOutputSchema || Array.isArray(structuredOutputSchema) || structuredOutputSchema.type !== "object") {
      throw new Error(`${ENV.structuredOutputSchema} must contain a root object JSON Schema`);
    }
  }
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
      if (["thinking", "reasoning_content"].includes(key.toLowerCase())) continue;
      output[key] = sanitizePublic(item, limit, depth + 1);
    }
    return output;
  }
  return value;
}

function finiteNonNegative(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
}

function summarizeMessages(rawMessages) {
  const messages = Array.isArray(rawMessages) ? rawMessages : [];
  let userMessages = 0;
  let assistantMessages = 0;
  let toolCalls = 0;
  let toolResults = 0;
  let input = 0;
  let output = 0;
  let cacheRead = 0;
  let cacheWrite = 0;
  let cost = 0;
  const calls = new Set();

  for (const entry of messages) {
    const info = entry?.info;
    if (info?.role === "user") userMessages += 1;
    if (info?.role === "assistant") {
      assistantMessages += 1;
      input += finiteNonNegative(info.tokens?.input);
      output += finiteNonNegative(info.tokens?.output) + finiteNonNegative(info.tokens?.reasoning);
      cacheRead += finiteNonNegative(info.tokens?.cache?.read);
      cacheWrite += finiteNonNegative(info.tokens?.cache?.write);
      cost += finiteNonNegative(info.cost);
    }
    for (const part of Array.isArray(entry?.parts) ? entry.parts : []) {
      if (part?.type !== "tool" || typeof part.callID !== "string" || !part.callID) continue;
      calls.add(part.callID);
      if (["completed", "error"].includes(part.state?.status)) toolResults += 1;
    }
  }
  toolCalls = calls.size;
  return {
    sessionId,
    userMessages,
    assistantMessages,
    toolCalls,
    toolResults,
    totalMessages: messages.length + toolResults,
    tokens: { input, output, cacheRead, cacheWrite, total: input + output + cacheRead + cacheWrite },
    cost,
  };
}

function stateFromStats(stats) {
  return {
    sessionId,
    messageCount: stats.totalMessages,
    pendingMessageCount: 0,
    isStreaming: false,
    isCompacting: false,
  };
}

function mappingPath() {
  return join(stateDir, "session-map", `${sessionId}.json`);
}

function invocationSessionStage() {
  return structuredOutputSchema ? SESSION_STAGE.planner : SESSION_STAGE.workspace;
}

function emptySessionMapping() {
  return {
    schemaVersion: SESSION_MAPPING_SCHEMA_VERSION,
    policyVersion: SESSION_POLICY_VERSION,
    fomoSessionId: sessionId,
    plannerSessionId: null,
    workspaceSessionId: null,
  };
}

function stageSessionKey(stage) {
  if (stage === SESSION_STAGE.planner) return "plannerSessionId";
  if (stage === SESSION_STAGE.workspace) return "workspaceSessionId";
  throw new OpenCodeCapabilityFailure({ reason: "unknown_session_stage" });
}

function validMappedSessionId(value) {
  return value === null || (typeof value === "string" && value.length > 0 && value.length <= 256);
}

function readSessionMapping() {
  const file = mappingPath();
  if (!existsSync(file)) return emptySessionMapping();
  const metadata = lstatSync(file);
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 4096) {
    throw new Error("OpenCode session mapping is not a bounded regular file");
  }
  let value;
  try {
    value = JSON.parse(readFileSync(file, "utf8"));
  } catch {
    throw new Error("OpenCode session mapping is invalid");
  }
  if (value?.schemaVersion === 1) {
    // Schema v1 points planning and workspace turns at one session. Planning
    // prompts in the old bridge persisted an all-deny tool override into that
    // session, so its capability state cannot be trusted or safely resumed.
    throw new OpenCodeCapabilityFailure({ reason: "legacy_session_mapping" });
  }
  if (
    !value || Array.isArray(value) ||
    Object.keys(value).sort().join(",") !==
      "fomoSessionId,plannerSessionId,policyVersion,schemaVersion,workspaceSessionId" ||
    value.schemaVersion !== SESSION_MAPPING_SCHEMA_VERSION ||
    value.policyVersion !== SESSION_POLICY_VERSION ||
    value.fomoSessionId !== sessionId ||
    !validMappedSessionId(value.plannerSessionId) ||
    !validMappedSessionId(value.workspaceSessionId) ||
    (value.plannerSessionId && value.plannerSessionId === value.workspaceSessionId)
  ) {
    throw new Error("OpenCode session mapping does not match the invocation");
  }
  return value;
}

function writeSessionMapping(mapping) {
  const file = mappingPath();
  const directory = dirname(file);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporary = join(directory, `.${sessionId}.${randomUUID()}.tmp`);
  writeFileSync(temporary, `${JSON.stringify(mapping)}\n`, { mode: 0o600, flag: "wx" });
  renameSync(temporary, file);
}

function configurePrivateEnvironment() {
  for (const subdirectory of ["data", "state", "cache"]) {
    mkdirSync(join(stateDir, subdirectory), { recursive: true, mode: 0o700 });
  }
  privateRuntimeDir = join(stateDir, `runtime-${randomUUID()}`);
  for (const subdirectory of ["config", "home", "tmp"]) {
    mkdirSync(join(privateRuntimeDir, subdirectory), { recursive: true, mode: 0o700 });
  }
  process.env.XDG_DATA_HOME = join(stateDir, "data");
  process.env.XDG_STATE_HOME = join(stateDir, "state");
  process.env.XDG_CACHE_HOME = join(stateDir, "cache");
  process.env.XDG_CONFIG_HOME = join(privateRuntimeDir, "config");
  process.env.OPENCODE_CONFIG_DIR = join(privateRuntimeDir, "config");
  process.env.OPENCODE_TEST_HOME = join(privateRuntimeDir, "home");
  process.env.TMPDIR = join(privateRuntimeDir, "tmp");
  process.env.OPENCODE_DISABLE_PROJECT_CONFIG = "1";
  process.env.OPENCODE_PURE = "1";
  process.env.OPENCODE_DISABLE_AUTOUPDATE = "1";
  process.env.OPENCODE_DISABLE_MODELS_FETCH = "1";
  process.env.OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER = "1";
  process.env.OPENCODE_EXPERIMENTAL = "0";
  delete process.env.OPENCODE_CONFIG;
  delete process.env.OPENCODE_PERMISSION;
  delete process.env.OPENCODE_PLUGIN_META_FILE;
  // The server is reachable only inside this process' sandbox loopback.
  // Remove inherited auth settings so the SDK client and server cannot drift.
  delete process.env.OPENCODE_SERVER_USERNAME;
  delete process.env.OPENCODE_SERVER_PASSWORD;
  process.env.PATH = `${dirname(openCodeBin)}:/usr/local/bin:/usr/bin:/bin`;
  delete process.env[ENV.prompt];
}

function reasoningVariants() {
  const variants = {};
  for (const level of model.thinkingLevels) {
    if (level === "default") continue;
    if (level === "off") {
      if (model.id === "fomo-pi-deepseek-flash") {
        variants.off = { reasoningEffort: "none" };
      }
      continue;
    }
    variants[level] = { reasoningEffort: level };
  }
  return variants;
}

function openCodeConfig() {
  return {
    model: `${PROVIDER_ID}/${model.id}`,
    small_model: `${PROVIDER_ID}/${model.id}`,
    default_agent: "build",
    share: "disabled",
    autoupdate: false,
    enabled_providers: [PROVIDER_ID],
    disabled_providers: [],
    plugin: [],
    mcp: {},
    instructions: [],
    formatter: false,
    lsp: false,
    snapshot: true,
    compaction: { auto: true, prune: true, preserve_recent_tokens: 20_000, reserved: 32_768 },
    permission: {
      read: "allow",
      edit: "allow",
      glob: "allow",
      grep: "allow",
      list: "allow",
      bash: "allow",
      todowrite: "allow",
      task: "deny",
      question: "deny",
      skill: "deny",
      webfetch: "deny",
      websearch: "deny",
      external_directory: "deny",
      doom_loop: "deny",
    },
    agent: {
      build: {
        mode: "primary",
        model: `${PROVIDER_ID}/${model.id}`,
        permission: {
          read: "allow", edit: "allow", glob: "allow", grep: "allow", list: "allow", bash: "allow",
          todowrite: "allow", task: "deny", question: "deny", skill: "deny", webfetch: "deny",
          websearch: "deny", external_directory: "deny", doom_loop: "deny",
        },
      },
    },
    provider: {
      [PROVIDER_ID]: {
        id: PROVIDER_ID,
        name: "FOMO run-scoped LiteLLM",
        npm: "@ai-sdk/openai-compatible",
        options: { baseURL: providerBaseUrl, apiKey: virtualKey, timeout: false },
        models: {
          [model.id]: {
            id: model.id,
            name: model.id,
            reasoning: true,
            tool_call: true,
            limit: {
              context: contextWindow,
              output: Math.min(model.maxOutputTokens, Math.max(1, contextWindow - 32_768)),
            },
            modalities: { input: ["text"], output: ["text"] },
            variants: reasoningVariants(),
          },
        },
      },
    },
  };
}

async function loadSdk() {
  let sdkPath = DEFAULT_SDK_PATH;
  if (process.env.NODE_ENV === "test" && process.env.FOMO_OPENCODE_SDK_PATH) {
    sdkPath = absolutePath("FOMO_OPENCODE_SDK_PATH", process.env.FOMO_OPENCODE_SDK_PATH);
  }
  return import(pathToFileURL(sdkPath).href);
}

function isKnownOpenCodeFailure(error) {
  return error instanceof OpenCodeModelFailure || error instanceof OpenCodeRuntimeFailure ||
    error instanceof OpenCodeCapabilityFailure;
}

function isModelResponseError(error) {
  return Boolean(
    error && typeof error === "object" && typeof error.name === "string" && MODEL_ERROR_NAMES.has(error.name),
  );
}

function modelFailure(cause) {
  return cause instanceof OpenCodeModelFailure ? cause : new OpenCodeModelFailure(cause);
}

function runtimeFailure(cause) {
  return isKnownOpenCodeFailure(cause) ? cause : new OpenCodeRuntimeFailure(cause);
}

function runtimeValue(operation) {
  try {
    return operation();
  } catch (error) {
    throw runtimeFailure(error);
  }
}

async function runtimeStep(operation) {
  try {
    return await operation();
  } catch (error) {
    throw runtimeFailure(error);
  }
}

function unwrap(result, operation) {
  if (result && typeof result === "object" && Object.hasOwn(result, "error") && result.error !== undefined) {
    throw new OpenCodeRuntimeFailure({ operation, sdkError: result.error });
  }
  const value = result && typeof result === "object" && Object.hasOwn(result, "data") ? result.data : result;
  if (value === undefined || value === null) throw new OpenCodeRuntimeFailure({ operation, reason: "missing_data" });
  return value;
}

function safeError(error) {
  if (error instanceof Error) return bounded(redact(error.message), 2048);
  if (error && typeof error === "object") {
    const message = error.data?.message ?? error.message ?? error.name;
    if (message) return bounded(redact(message), 2048);
  }
  return bounded(redact(String(error ?? "unknown error")), 2048);
}

function sessionPolicyMetadata(stage) {
  return {
    fomoSessionStage: stage,
    fomoSessionPolicyVersion: SESSION_POLICY_VERSION,
  };
}

function permissionMatches(candidate, required) {
  return candidate === required || candidate === "*";
}

function effectivePermission(permission, name) {
  if (!Array.isArray(permission)) return null;
  for (let index = permission.length - 1; index >= 0; index -= 1) {
    const rule = permission[index];
    if (
      rule && typeof rule === "object" && rule.pattern === "*" &&
      permissionMatches(rule.permission, name) && ["allow", "deny", "ask"].includes(rule.action)
    ) {
      return rule.action;
    }
  }
  return null;
}

function assertSessionPolicy(session, stage) {
  if (!session || typeof session !== "object" || typeof session.id !== "string" || !session.id) {
    throw new OpenCodeCapabilityFailure({ reason: "invalid_stage_session" });
  }
  const metadata = session.metadata;
  if (
    !metadata || metadata.fomoSessionStage !== stage ||
    metadata.fomoSessionPolicyVersion !== SESSION_POLICY_VERSION
  ) {
    throw new OpenCodeCapabilityFailure({ reason: "stage_session_identity_mismatch" });
  }
  const expected = SESSION_PERMISSION_PROFILES[stage];
  if (
    !Array.isArray(session.permission) || session.permission.length !== expected.length ||
    expected.some((rule, index) => {
      const actual = session.permission[index];
      return !actual || actual.permission !== rule.permission ||
        actual.pattern !== rule.pattern || actual.action !== rule.action;
    })
  ) {
    throw new OpenCodeCapabilityFailure({ reason: "stage_session_permission_mismatch" });
  }
}

async function inspectCapabilities(client, session, stage) {
  assertSessionPolicy(session, stage);
  if (!client.tool || typeof client.tool.list !== "function") {
    throw new OpenCodeCapabilityFailure({ reason: "tool_registry_unavailable" });
  }
  let tools;
  try {
    tools = unwrap(
      await client.tool.list({ directory: workspace, provider: PROVIDER_ID, model: model.id }),
      "OpenCode tool registry query",
    );
  } catch {
    throw new OpenCodeCapabilityFailure({ reason: "tool_registry_query_failed" });
  }
  if (
    !Array.isArray(tools) || tools.some((tool) =>
      !tool || typeof tool !== "object" || typeof tool.id !== "string" || !tool.id)
  ) {
    throw new OpenCodeCapabilityFailure({ reason: "invalid_tool_registry" });
  }
  const toolIds = new Set(tools.map((tool) => tool.id));
  const capabilities = {
    structuredOutput: stage === SESSION_STAGE.planner &&
      structuredOutputSchema !== null && typeof client.session?.prompt === "function",
    repoRead: effectivePermission(session.permission, "read") === "allow" && toolIds.has("read"),
    repoMutate: effectivePermission(session.permission, "edit") === "allow" &&
      ["apply_patch", "edit", "write", "patch"].some((tool) => toolIds.has(tool)),
    commandExec: effectivePermission(session.permission, "bash") === "allow" && toolIds.has("bash"),
    sessionResume: typeof client.session?.get === "function" && typeof client.session?.messages === "function" &&
      (stage === SESSION_STAGE.planner ||
        (typeof client.session?.promptAsync === "function" && typeof client.session?.status === "function")),
    sessionCancel: typeof client.session?.abort === "function",
  };
  const required = stage === SESSION_STAGE.planner
    ? ["structuredOutput", "sessionResume", "sessionCancel"]
    : ["repoRead", "repoMutate", "commandExec", "sessionResume", "sessionCancel"];
  if (required.some((capability) => capabilities[capability] !== true)) {
    throw new OpenCodeCapabilityFailure({ reason: "required_capability_missing", stage });
  }
  return capabilities;
}

async function resolveSession(client) {
  const stage = invocationSessionStage();
  const mapping = runtimeValue(() => readSessionMapping());
  const sessionKey = stageSessionKey(stage);
  const mapped = mapping[sessionKey];
  if (mapped) {
    try {
      const existing = unwrap(
        await client.session.get({ sessionID: mapped, directory: workspace }),
        "OpenCode session lookup",
      );
      if (existing.id !== mapped) throw new OpenCodeRuntimeFailure({ reason: "session_mismatch" });
      assertSessionPolicy(existing, stage);
      return { id: mapped, resumed: true, stage, session: existing };
    } catch (error) {
      throw runtimeFailure(error);
    }
  }
  if (requireResume) throw new OpenCodeRuntimeFailure({ reason: "resume_unavailable" });
  const created = unwrap(
    await client.session.create({
      directory: workspace,
      title: `FOMO ${correlationId}`,
      agent: "build",
      model: { providerID: PROVIDER_ID, id: model.id, ...(thinkingVariant() ? { variant: thinkingVariant() } : {}) },
      metadata: sessionPolicyMetadata(stage),
      permission: SESSION_PERMISSION_PROFILES[stage].map((rule) => ({ ...rule })),
    }),
    "OpenCode session creation",
  );
  if (!created || typeof created.id !== "string" || !created.id) {
    throw new OpenCodeRuntimeFailure({ reason: "invalid_session" });
  }
  const otherSessionKey = stage === SESSION_STAGE.planner ? "workspaceSessionId" : "plannerSessionId";
  if (mapping[otherSessionKey] === created.id) {
    throw new OpenCodeCapabilityFailure({ reason: "stage_session_collision" });
  }
  const persisted = unwrap(
    await client.session.get({ sessionID: created.id, directory: workspace }),
    "OpenCode created session lookup",
  );
  if (persisted.id !== created.id) throw new OpenCodeRuntimeFailure({ reason: "session_mismatch" });
  assertSessionPolicy(persisted, stage);
  const nextMapping = { ...mapping, [sessionKey]: created.id };
  runtimeValue(() => writeSessionMapping(nextMapping));
  return { id: created.id, resumed: false, stage, session: persisted };
}

function thinkingVariant() {
  if (thinkingLevel === "default") return null;
  if (thinkingLevel === "off" && model.id !== "fomo-pi-deepseek-flash") return null;
  return thinkingLevel;
}

function eventSessionId(event) {
  const properties = event?.properties;
  if (!properties || typeof properties !== "object") return null;
  if (typeof properties.sessionID === "string") return properties.sessionID;
  if (typeof properties.part?.sessionID === "string") return properties.part.sessionID;
  if (typeof properties.info?.sessionID === "string") return properties.info.sessionID;
  return null;
}

function eventBelongsToSession(event) {
  // OpenCode's legacy session.error schema permits an absent sessionID. The
  // server can own more than one durable stage session, so an unscoped error
  // must never be attributed to the active turn. A scoped assistant error is
  // recovered from message history during the terminal reconciliation.
  return eventSessionId(event) === sdkSessionId;
}

function touchActivity() {
  // Receipt of a reasoning event proves liveness, but its content is never emitted.
  eventCount += 1;
}

function beginTurn(messageId) {
  const id = String(messageId ?? "");
  if (!id || startedTurns.has(id)) return;
  startedTurns.add(id);
  sawTurn = true;
  emitPi("turn_start");
  emitPi("message_start", { role: "assistant" });
}

function endTurn(messageId, finish, text = "") {
  const id = String(messageId ?? "");
  if (!id || endedTurns.has(id)) return;
  beginTurn(id);
  endedTurns.add(id);
  const stopReason = String(finish ?? "stop");
  lastStopReason = stopReason;
  emitPi("message_end", { role: "assistant", stopReason });
  emitPi("turn_end", {
    role: "assistant",
    stopReason,
    text: bounded(redact(text), LIMITS.publicTextCharacters),
    toolResults: [],
  });
}

function beginTool(callId, toolName, args) {
  const id = String(callId ?? "");
  const name = String(toolName ?? "");
  if (!id || !name || activeTools.has(id) || completedTools.has(id)) return;
  activeTools.set(id, name);
  if (firstToolElapsedMs === null && runStartedAt) firstToolElapsedMs = Date.now() - runStartedAt;
  toolCounts[name] = (toolCounts[name] ?? 0) + 1;
  emitPi("tool_start", {
    toolCallId: id,
    toolName: name,
    args: sanitizePublic(args ?? {}),
    elapsedMs: runStartedAt ? Date.now() - runStartedAt : null,
  });
}

function progressTool(callId, text) {
  const id = String(callId ?? "");
  const name = activeTools.get(id);
  if (!name) return;
  emitPi("tool_output", {
    toolCallId: id,
    toolName: name,
    text: bounded(redact(text), LIMITS.publicTextCharacters),
    cumulative: true,
    elapsedMs: runStartedAt ? Date.now() - runStartedAt : null,
  });
}

function endTool(callId, isError) {
  const id = String(callId ?? "");
  const name = activeTools.get(id);
  if (!name) return;
  activeTools.delete(id);
  completedTools.add(id);
  if (
    !isError && ["edit", "write", "patch", "apply_patch"].includes(name) &&
    firstEditOrWriteToolElapsedMs === null && runStartedAt
  ) {
    firstEditOrWriteToolElapsedMs = Date.now() - runStartedAt;
  }
  emitPi("tool_end", {
    toolCallId: id,
    toolName: name,
    isError: isError === true,
    elapsedMs: runStartedAt ? Date.now() - runStartedAt : null,
  });
}

function publicToolContent(content) {
  if (!Array.isArray(content)) return "";
  return content
    .filter((part) => part && typeof part === "object" && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n");
}

function invocationAssistant(messageId) {
  const id = String(messageId ?? "");
  return Boolean(id && workspaceInvocation?.assistantIds.has(id));
}

function emitCompletedText(part) {
  const id = String(part?.id ?? "");
  const messageId = String(part?.messageID ?? "");
  if (!id || !messageId || completedTexts.has(id) || !invocationAssistant(messageId)) return;
  beginTurn(messageId);
  if (!startedTexts.has(id)) {
    startedTexts.add(id);
    emitPi("message_delta", { deltaType: "text_start", contentIndex: 0 });
    const text = bounded(redact(part?.text), LIMITS.publicTextCharacters);
    if (text) {
      sawPublicTextDelta = true;
      emitPi("message_delta", { deltaType: "text_delta", contentIndex: 0, delta: text });
    }
  }
  emitPi("message_delta", { deltaType: "text_end", contentIndex: 0 });
  completedTexts.add(id);
}

function handleLegacyPart(part, delta = "") {
  if (!part || typeof part !== "object" || !invocationAssistant(part.messageID)) return;
  workspaceInvocation.sawActivity = true;
  workspaceInvocation.parts.set(String(part.id ?? ""), part);
  const messageId = String(part.messageID ?? "");

  if (part.type === "step-start") {
    beginTurn(messageId);
    return;
  }
  if (part.type === "step-finish") {
    endTurn(messageId, part.reason || "stop");
    return;
  }
  if (part.type === "retry") {
    emitPi("auto_retry_start", { attempt: Number(part.attempt ?? 0), maxAttempts: 0 });
    return;
  }
  if (part.type === "compaction") {
    emitPi("compaction_start", { reason: part.auto ? "auto" : "manual" });
    return;
  }
  if (part.type === "text") {
    const id = String(part.id ?? "");
    if (delta && id && !completedTexts.has(id)) {
      beginTurn(messageId);
      if (!startedTexts.has(id)) {
        startedTexts.add(id);
        emitPi("message_delta", { deltaType: "text_start", contentIndex: 0 });
      }
      sawPublicTextDelta = true;
      emitPi("message_delta", {
        deltaType: "text_delta",
        contentIndex: 0,
        delta: bounded(redact(delta), LIMITS.publicDeltaCharacters),
      });
    }
    if (part.time?.end) emitCompletedText(part);
    return;
  }
  if (part.type !== "tool") return;

  const callId = String(part.callID || part.id || "");
  const state = part.state ?? {};
  if (["running", "completed", "error"].includes(state.status)) {
    beginTurn(messageId);
    beginTool(callId, part.tool, state.input);
  }
  if (state.status === "completed") {
    progressTool(callId, state.output);
    endTool(callId, false);
  } else if (state.status === "error") {
    progressTool(callId, state.error);
    endTool(callId, true);
  }
}

function handleSdkEvent(event) {
  if (!event || typeof event !== "object" || typeof event.type !== "string") return;
  if (event.type === "server.connected") return;
  if (lifecycle !== "running") return;
  if (!eventBelongsToSession(event)) return;
  touchActivity();
  const properties = event.properties ?? {};

  if (event.type === "session.error") {
    terminalError = isModelResponseError(properties.error)
      ? modelFailure(properties.error)
      : new OpenCodeRuntimeFailure({ reason: "session_error" });
    if (workspaceInvocation) workspaceInvocation.sawActivity = true;
    return;
  }

  // Structured planning is represented by one synthetic terminating tool
  // after SDK schema validation. Native OpenCode tools/text are never exposed.
  if (structuredOutputSchema) return;

  switch (event.type) {
    case "message.updated": {
      const info = properties.info;
      if (!workspaceInvocation || !info || typeof info !== "object") break;
      if (info.role === "user" && info.id === workspaceInvocation.messageId) {
        workspaceInvocation.sawActivity = true;
      }
      if (info.role === "assistant" && info.parentID === workspaceInvocation.messageId) {
        workspaceInvocation.sawActivity = true;
        workspaceInvocation.assistantIds.add(String(info.id));
        workspaceInvocation.assistants.set(String(info.id), info);
        beginTurn(info.id);
        if (info.error) {
          terminalError = isModelResponseError(info.error)
            ? modelFailure(info.error)
            : new OpenCodeRuntimeFailure({ reason: "assistant_error" });
        }
      }
      break;
    }
    case "message.part.updated":
      handleLegacyPart(properties.part, properties.delta);
      break;
    case "message.part.delta": {
      if (!workspaceInvocation || !invocationAssistant(properties.messageID)) break;
      const prior = workspaceInvocation.parts.get(String(properties.partID ?? ""));
      if (prior && properties.field === "text") handleLegacyPart(prior, properties.delta);
      break;
    }
    case "session.status": {
      if (!workspaceInvocation) break;
      const status = String(properties.status?.type ?? "");
      workspaceInvocation.status = status;
      if (["busy", "retry"].includes(status)) {
        workspaceInvocation.sawActivity = true;
        workspaceInvocation.sawBusy = true;
      }
      if (status === "idle") workspaceInvocation.sawIdle = true;
      break;
    }
    case "session.idle":
      if (workspaceInvocation) workspaceInvocation.sawIdle = true;
      break;
    case "session.next.step.started":
      beginTurn(properties.assistantMessageID);
      break;
    case "session.next.step.ended":
      endTurn(properties.assistantMessageID, properties.finish);
      break;
    case "session.next.step.failed":
      terminalError = isModelResponseError(properties.error)
        ? modelFailure(properties.error)
        : new OpenCodeRuntimeFailure({ reason: "session_step_failed" });
      break;
    case "session.next.text.started": {
      beginTurn(properties.assistantMessageID);
      const textId = String(properties.textID ?? "");
      if (textId && !startedTexts.has(textId)) {
        startedTexts.add(textId);
        emitPi("message_delta", { deltaType: "text_start", contentIndex: 0 });
      }
      break;
    }
    case "session.next.text.delta":
      beginTurn(properties.assistantMessageID);
      sawPublicTextDelta = true;
      emitPi("message_delta", {
        deltaType: "text_delta",
        contentIndex: 0,
        delta: bounded(redact(properties.delta), LIMITS.publicDeltaCharacters),
      });
      break;
    case "session.next.text.ended":
      emitPi("message_delta", { deltaType: "text_end", contentIndex: 0 });
      break;
    case "session.next.tool.called":
      beginTurn(properties.assistantMessageID);
      beginTool(properties.callID, properties.tool, properties.input);
      break;
    case "session.next.tool.progress":
      progressTool(properties.callID, publicToolContent(properties.content));
      break;
    case "session.next.tool.success":
      progressTool(properties.callID, publicToolContent(properties.content));
      endTool(properties.callID, false);
      break;
    case "session.next.tool.failed":
      progressTool(properties.callID, safeError(properties.error));
      endTool(properties.callID, true);
      break;
    case "session.next.retried":
      emitPi("auto_retry_start", { attempt: Number(properties.attempt ?? 0), maxAttempts: 0 });
      break;
    case "session.next.compaction.started":
      emitPi("compaction_start", { reason: String(properties.reason ?? "auto") });
      break;
    case "session.next.compaction.ended":
      emitPi("compaction_end", { reason: String(properties.reason ?? "auto"), aborted: false, willRetry: false });
      break;
    default:
      // Reasoning deltas and compatibility projections are intentionally
      // consumed only as private liveness. Unknown SDK events are ignored.
      break;
  }
}

async function startEventStream(client) {
  sseAbortController = new AbortController();
  const subscription = await runtimeStep(() => client.event.subscribe(
    { directory: workspace },
    { signal: sseAbortController.signal, sseMaxRetryAttempts: 1 },
  ));
  if (!subscription?.stream || typeof subscription.stream[Symbol.asyncIterator] !== "function") {
    throw new OpenCodeRuntimeFailure({ reason: "invalid_sse_stream" });
  }
  let readyResolve;
  let readyReject;
  let becameReady = false;
  const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const task = (async () => {
    try {
      for await (const event of subscription.stream) {
        becameReady = true;
        readyResolve();
        handleSdkEvent(event);
      }
      if (!becameReady) readyReject(new OpenCodeRuntimeFailure({ reason: "sse_not_ready" }));
      else if (!sseAbortController.signal.aborted && lifecycle === "running") {
        terminalError = new OpenCodeRuntimeFailure({ reason: "sse_ended_early" });
      }
    } catch (error) {
      if (!sseAbortController.signal.aborted) {
        const failure = runtimeFailure(error);
        readyReject(failure);
        terminalError = failure;
        diagnostic("OpenCode SSE ended unexpectedly");
      }
    }
  })();
  await Promise.race([
    ready,
    new Promise((_, reject) => setTimeout(
      () => reject(new OpenCodeRuntimeFailure({ reason: "sse_ready_timeout" })),
      5000,
    )),
  ]);
  // Wrap the promise so this async function does not adopt it and wait for
  // stream termination before the prompt is submitted.
  return { task };
}

function assertAsyncPromptAccepted(result) {
  if (result && typeof result === "object" && Object.hasOwn(result, "error") && result.error !== undefined) {
    throw new OpenCodeRuntimeFailure({ operation: "OpenCode async prompt", reason: "sdk_error" });
  }
  const status = result?.response?.status;
  if (status !== undefined && status !== 204) {
    throw new OpenCodeRuntimeFailure({ operation: "OpenCode async prompt", reason: "unexpected_status" });
  }
}

function inspectWorkspaceHistory(messages, messageId) {
  const entries = Array.isArray(messages) ? messages : [];
  const userPresent = entries.some((entry) => entry?.info?.role === "user" && entry.info.id === messageId);
  const assistants = entries.filter((entry) =>
    entry?.info?.role === "assistant" && entry.info.parentID === messageId);
  const last = assistants.at(-1) ?? null;
  const pendingTools = assistants.flatMap((entry) => Array.isArray(entry?.parts) ? entry.parts : [])
    .filter((part) => part?.type === "tool" && ["pending", "running"].includes(part.state?.status));
  return {
    userPresent,
    assistants,
    last,
    pendingTools,
    completed: Boolean(last?.info?.time?.completed && !last.info.error && pendingTools.length === 0),
  };
}

function emitReconciledTools(response) {
  for (const part of Array.isArray(response?.parts) ? response.parts : []) {
    if (part?.type !== "tool" || !["completed", "error"].includes(part.state?.status)) continue;
    const callId = String(part.callID || part.id || "");
    beginTurn(part.messageID);
    beginTool(callId, part.tool, part.state?.input);
    progressTool(callId, part.state?.status === "completed" ? part.state?.output : part.state?.error);
    endTool(callId, part.state?.status === "error");
  }
}

async function querySessionMessages(client, operation) {
  const messages = unwrap(
    await runtimeStep(() => client.session.messages({ sessionID: sdkSessionId, directory: workspace })),
    operation,
  );
  if (!Array.isArray(messages)) throw new OpenCodeRuntimeFailure({ reason: "invalid_session_messages" });
  return messages;
}

async function querySessionStatus(client) {
  const statuses = unwrap(
    await runtimeStep(() => client.session.status({ directory: workspace })),
    "OpenCode session status query",
  );
  if (!statuses || typeof statuses !== "object" || Array.isArray(statuses)) {
    throw new OpenCodeRuntimeFailure({ reason: "invalid_session_status" });
  }
  const status = statuses[sdkSessionId]?.type ?? "idle";
  if (!["idle", "busy", "retry"].includes(status)) {
    throw new OpenCodeRuntimeFailure({ reason: "unknown_session_status" });
  }
  return status;
}

async function runWorkspacePrompt(client) {
  const messageId = `msg_fomo_${randomUUID().replaceAll("-", "")}`;
  workspaceInvocation = {
    messageId,
    accepted: false,
    sawActivity: false,
    sawBusy: false,
    sawIdle: false,
    status: "submitting",
    assistantIds: new Set(),
    assistants: new Map(),
    parts: new Map(),
  };

  const accepted = await runtimeStep(() => client.session.promptAsync({
    sessionID: sdkSessionId,
    directory: workspace,
    messageID: messageId,
    model: { providerID: PROVIDER_ID, modelID: model.id },
    agent: "build",
    ...(thinkingVariant() ? { variant: thinkingVariant() } : {}),
    parts: [{ type: "text", text: prompt }],
  }));
  assertAsyncPromptAccepted(accepted);
  workspaceInvocation.accepted = true;

  let idleMisses = 0;
  let admissionMisses = 0;
  while (lifecycle === "running") {
    const status = await querySessionStatus(client);
    workspaceInvocation.status = status;
    if (["busy", "retry"].includes(status)) {
      workspaceInvocation.sawBusy = true;
      workspaceInvocation.sawActivity = true;
      idleMisses = 0;
    } else {
      const messages = await querySessionMessages(client, "OpenCode workspace reconciliation");
      const result = inspectWorkspaceHistory(messages, messageId);
      if (result.completed) {
        emitReconciledTools(result.last);
        return { response: result.last, finalMessages: messages };
      }
      if (result.last?.info?.error) throw modelFailure(result.last.info.error);
      if (result.pendingTools.length || result.userPresent ||
          workspaceInvocation.sawActivity || workspaceInvocation.sawBusy) {
        idleMisses += 1;
      } else {
        admissionMisses += 1;
      }
      // Idle is authoritative, but its event can race the final durable
      // message write. These retries only settle persistence; they do not cap
      // model execution time.
      if (idleMisses >= 4) {
        if (terminalError) throw terminalError;
        if (result.pendingTools.length) throw new OpenCodeRuntimeFailure({ reason: "unfinished_tool_calls" });
        throw new OpenCodeRuntimeFailure({ reason: "missing_completed_assistant" });
      }
      if (admissionMisses >= 40) {
        if (terminalError) throw terminalError;
        throw new OpenCodeRuntimeFailure({ reason: "async_prompt_not_persisted" });
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new OpenCodeRuntimeFailure({ reason: "workspace_prompt_interrupted" });
}

function finalText(response) {
  return (Array.isArray(response?.parts) ? response.parts : [])
    .filter((part) => part?.type === "text" && typeof part.text === "string" && !part.ignored)
    .map((part) => part.text)
    .join("\n")
    .trim();
}

function emitFinalFallback(response) {
  const info = response?.info ?? {};
  let messageId = String(info.id || `assistant-${randomUUID()}`);
  const text = finalText(response);
  if (!sawTurn || (!sawPublicTextDelta && text)) {
    if (endedTurns.has(messageId)) messageId = `${messageId}-public`;
    beginTurn(messageId);
    if (text) {
      emitPi("message_delta", { deltaType: "text_start", contentIndex: 0 });
      for (let offset = 0; offset < text.length; offset += LIMITS.publicDeltaCharacters) {
        emitPi("message_delta", {
          deltaType: "text_delta",
          contentIndex: 0,
          delta: bounded(redact(text.slice(offset, offset + LIMITS.publicDeltaCharacters)), LIMITS.publicDeltaCharacters),
        });
      }
      emitPi("message_delta", { deltaType: "text_end", contentIndex: 0 });
    }
    endTurn(messageId, info.finish || "stop", text);
  } else if (!endedTurns.has(messageId) && startedTurns.has(messageId)) {
    endTurn(messageId, info.finish || "stop", text);
  }
}

function emitStructuredResult(response) {
  const value = response?.info?.structured;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new OpenCodeModelFailure({ reason: "missing_structured_output" });
  }
  let safe;
  try {
    safe = JSON.parse(redact(JSON.stringify(value)));
  } catch {
    throw new OpenCodeModelFailure({ reason: "invalid_structured_output" });
  }
  const messageId = String(response?.info?.id || `assistant-${randomUUID()}`);
  const callId = `structured-${randomUUID()}`;
  beginTurn(messageId);
  beginTool(callId, STRUCTURED_OUTPUT_TOOL, safe);
  endTool(callId, false);
  endTurn(messageId, response?.info?.finish || "stop", "");
}

function startHeartbeat() {
  const cadence = Math.max(250, Math.min(15_000, ((activitySilenceSeconds ?? 30) * 1000) / 2));
  heartbeatHandle = setInterval(() => {
    if (lifecycle === "running") emitPi("inference_heartbeat");
  }, cadence);
}

function stopTimers() {
  if (timeoutHandle) clearTimeout(timeoutHandle);
  timeoutHandle = null;
  if (heartbeatHandle) clearInterval(heartbeatHandle);
  heartbeatHandle = null;
}

async function abortSession() {
  if (!sdkClient || !sdkSessionId) return;
  try {
    await Promise.race([
      sdkClient.session.abort({ sessionID: sdkSessionId, directory: workspace }),
      new Promise((resolve) => setTimeout(resolve, Math.min(2000, graceSeconds * 1000))),
    ]);
  } catch {
    diagnostic("cannot abort OpenCode session");
  }
}

async function cleanup({ abort = true } = {}) {
  stopTimers();
  if (sseAbortController) sseAbortController.abort();
  if (abort) await abortSession();
  try { sdkServer?.close(); } catch { diagnostic("cannot stop OpenCode server"); }
  sdkServer = null;
  if (privateRuntimeDir) {
    try { rmSync(privateRuntimeDir, { recursive: true, force: true }); } catch { /* best effort */ }
    privateRuntimeDir = null;
  }
}

async function fail(code, exitCode = 1) {
  if (["failed", "completed"].includes(lifecycle)) return;
  const phase = lifecycle;
  lifecycle = "failed";
  const stableCode = Object.hasOwn(BRIDGE_FAILURE_MESSAGES, code) ? code : "opencode_failed";
  emit("failed", { code: stableCode, message: BRIDGE_FAILURE_MESSAGES[stableCode], phase });
  await cleanup();
  process.exit(exitCode);
}

async function main() {
  try {
    runtimeValue(() => parseEnvironment());
    runtimeValue(() => {
      mkdirSync(stateDir, { recursive: true, mode: 0o700 });
      const stateMetadata = lstatSync(stateDir);
      if (!stateMetadata.isDirectory() || stateMetadata.isSymbolicLink()) {
        throw new Error(`${ENV.stateDir} is not a safe directory`);
      }
      configurePrivateEnvironment();
    });
    const sdk = await runtimeStep(() => loadSdk());
    if (typeof sdk.createOpencodeServer !== "function" || typeof sdk.createOpencodeClient !== "function") {
      throw new OpenCodeRuntimeFailure({ reason: "sdk_v2_unavailable" });
    }
    const abortController = new AbortController();
    sdkServer = await runtimeStep(() => sdk.createOpencodeServer({
      hostname: "127.0.0.1",
      port: 0,
      timeout: 10_000,
      signal: abortController.signal,
      config: openCodeConfig(),
    }));
    if (!sdkServer?.url || !String(sdkServer.url).startsWith("http://127.0.0.1:")) {
      throw new OpenCodeRuntimeFailure({ reason: "non_loopback_server" });
    }
    sdkClient = runtimeValue(() => sdk.createOpencodeClient({ baseUrl: sdkServer.url, directory: workspace }));
    const resolved = await runtimeStep(() => resolveSession(sdkClient));
    sdkSessionId = resolved.id;
    const capabilities = await inspectCapabilities(sdkClient, resolved.session, resolved.stage);
    let initialMessages;
    let initialHistoryAvailable = true;
    try {
      initialMessages = unwrap(
        await runtimeStep(() => sdkClient.session.messages({ sessionID: sdkSessionId, directory: workspace })),
        "OpenCode initial message query",
      );
      if (!Array.isArray(initialMessages)) throw new OpenCodeRuntimeFailure({ reason: "invalid_initial_messages" });
    } catch (error) {
      if (!resolved.resumed || resolved.stage !== "planner") throw error;
      // session.get() already established that the mapped durable session
      // exists. The pinned OpenCode version may fail to project a structured
      // planner turn through the history endpoint. This exception is never
      // allowed for a workspace/build session: repair resume must prove its
      // readable history before the model is called.
      initialHistoryAvailable = false;
      initialMessages = [];
      diagnostic("OpenCode resume history telemetry unavailable; deferring validation to resumed prompt");
    }
    if (requireResume && initialHistoryAvailable && initialMessages.length === 0) {
      throw new OpenCodeRuntimeFailure({ reason: "empty_resume_session" });
    }
    const initialStats = summarizeMessages(initialMessages);

    lifecycle = "running";
    runStartedAt = Date.now();
    emit("started", {
      sessionId,
      model: modelRef,
      thinkingLevel,
      contextWindow,
      resumed: resolved.resumed && (!initialHistoryAvailable || initialMessages.length > 0),
      initialStats,
      capabilities,
    });
    emitPi("agent_start");
    startHeartbeat();
    if (timeoutSeconds !== null) {
      timeoutHandle = setTimeout(() => void fail("timeout", 124), timeoutSeconds * 1000);
    }

    const { task: eventTask } = await startEventStream(sdkClient);
    let response;
    let finalMessages;
    if (structuredOutputSchema) {
      response = unwrap(
        await runtimeStep(() => sdkClient.session.prompt({
          sessionID: sdkSessionId,
          directory: workspace,
          model: { providerID: PROVIDER_ID, modelID: model.id },
          agent: "build",
          ...(thinkingVariant() ? { variant: thinkingVariant() } : {}),
          format: { type: "json_schema", schema: structuredOutputSchema, retryCount: 2 },
          system: "Return only the object required by the active structured-output schema. Do not modify files or emit prose.",
          parts: [{ type: "text", text: prompt }],
        })),
        "OpenCode structured prompt",
      );
    } else {
      ({ response, finalMessages } = await runWorkspacePrompt(sdkClient));
    }
    if (!response?.info || response.info.role !== "assistant") {
      throw new OpenCodeRuntimeFailure({ reason: "invalid_assistant_message" });
    }
    if (response.info.error) throw modelFailure(response.info.error);
    if (structuredOutputSchema && terminalError) throw terminalError;
    if (activeTools.size) throw new OpenCodeRuntimeFailure({ reason: "unfinished_tool_calls" });

    if (structuredOutputSchema) emitStructuredResult(response);
    else emitFinalFallback(response);
    if (!sawTurn) throw new OpenCodeModelFailure({ reason: "missing_public_result" });

    settled = true;
    emitPi("agent_end", { willRetry: false });
    emitPi("agent_settled");
    lifecycle = "finalizing";
    stopTimers();
    if (sseAbortController) sseAbortController.abort();
    await Promise.race([eventTask, new Promise((resolve) => setTimeout(resolve, 100))]);

    if (!finalMessages) {
      try {
        finalMessages = await querySessionMessages(sdkClient, "OpenCode final message query");
      } catch {
        // The synchronous structured prompt response is authoritative. The
        // async workspace path never takes this fallback because its success
        // contract already requires durable message reconciliation.
        diagnostic("OpenCode final message telemetry unavailable; using completed-turn fallback");
        finalMessages = [
          ...initialMessages,
          { info: { role: "user" }, parts: [] },
          response,
        ];
      }
    }
    const stats = summarizeMessages(finalMessages);
    const state = stateFromStats(stats);
    await cleanup({ abort: false });
    lifecycle = "completed";
    emit("completed", {
      sessionId,
      state,
      stats,
      inputRequest: null,
      telemetry: {
        firstToolElapsedMs,
        firstEditOrWriteToolElapsedMs,
        toolCounts: { ...toolCounts },
        lastStopReason,
        openCodeEventCount: eventCount,
      },
    });
    process.exit(0);
  } catch (error) {
    const code = error instanceof OpenCodeModelFailure
      ? "opencode_model_failed"
      : error instanceof OpenCodeCapabilityFailure
        ? "opencode_capability_unavailable"
        : error instanceof OpenCodeRuntimeFailure
          ? "opencode_runtime_failed"
          : "opencode_failed";
    await fail(code, 1);
  }
}

async function onSignal(signal, code) {
  if (shuttingDown || ["failed", "completed"].includes(lifecycle)) return;
  shuttingDown = true;
  await fail("terminated", code);
}

process.on("SIGTERM", () => void onSignal("SIGTERM", 143));
process.on("SIGINT", () => void onSignal("SIGINT", 130));
process.on("uncaughtException", () => void fail("bridge_error", 1));
process.on("unhandledRejection", () => void fail("bridge_error", 1));

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) void main();

export { sanitizePublic, summarizeMessages };
