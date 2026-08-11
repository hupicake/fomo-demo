#!/usr/bin/env node
/**
 * Root-owned Codex CLI adapter for FOMO's existing strict bridge protocol.
 *
 * Prompts and run-scoped keys never enter argv. Codex native JSONL is reduced
 * to the public PiBridge envelope so FOMO can reuse one orchestrator, event
 * projection, usage ledger, cancellation path, and verification pipeline.
 */

import { spawn, spawnSync } from "node:child_process";
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { isDeepStrictEqual, TextDecoder } from "node:util";

const SCHEMA_VERSION = 1;
const CODEX_VERSION = "codex-cli 0.147.0";
const CODEX_MODEL_CATALOG = "/opt/fomo/bin/fomo-codex-models.json";
const STRUCTURED_OUTPUT_TOOL = "submit_structured_output";
const COMMAND_FAILURE_RECOVERY_PROMPT =
  "Continue the current task from the persisted thread. Inspect the failed command result, correct the issue, and finish the requested work without repeating completed work.";
const MODEL_REFS = new Set([
  "fomo-litellm/fomo-pi-gpt-5.5",
  "fomo-litellm/fomo-pi-gpt-5.6",
]);
const THINKING_MAP = new Map([
  ["low", "low"],
  ["medium", "medium"],
  ["high", "high"],
  ["xhigh", "xhigh"],
]);
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;
const SESSION_ID = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const MAX = Object.freeze({
  identifier: 128,
  promptCharacters: 100_000,
  keyCharacters: 4_096,
  schemaBytes: 64 * 1024,
  stdoutBytes: 32 * 1024 * 1024,
  lineBytes: 16 * 1024 * 1024,
  publicTextCharacters: 16_000,
  assistantCharacters: 4 * 1024 * 1024,
});
const STRICT_UNSUPPORTED_SCHEMA_KEYWORDS = new Set([
  "default", "minLength", "maxLength", "pattern", "format",
  "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum",
  "exclusiveMaximum", "multipleOf", "minProperties", "maxProperties",
  "patternProperties", "unevaluatedProperties", "propertyNames", "contains",
  "minContains", "maxContains", "dependentRequired", "dependentSchemas",
]);
const fatalUtf8 = new TextDecoder("utf-8", { fatal: true });

const ENV = Object.freeze({
  prompt: "FOMO_PI_PROMPT_B64",
  sessionId: "FOMO_PI_SESSION_ID",
  requestId: "FOMO_PI_REQUEST_ID",
  correlationId: "FOMO_PI_CORRELATION_ID",
  providerBaseUrl: "FOMO_PI_PROVIDER_BASE_URL",
  virtualKey: "FOMO_PI_VIRTUAL_KEY",
  workspace: "FOMO_PI_WORKSPACE",
  stateDir: "FOMO_PI_STATE_DIR",
  codexBin: "FOMO_PI_BIN",
  thinking: "FOMO_PI_THINKING_LEVEL",
  modelRef: "FOMO_PI_MODEL_REF",
  contextWindow: "FOMO_PI_CONTEXT_WINDOW",
  schema: "FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64",
  userInput: "FOMO_PI_USER_INPUT_ENABLED",
  requireResume: "FOMO_PI_REQUIRE_RESUME",
});

let requestId = "invalid-request";
let correlationId = "invalid-run";
let sessionId = "invalid-session";
let prompt = "";
let promptBase64 = "";
let virtualKey = "";
let providerBaseUrl = "";
let workspace = "";
let stateDir = "";
let codexBin = "";
let modelRef = "";
let modelAlias = "";
let thinkingLevel = "";
let codexThinking = "";
let contextWindow = 0;
let schemaText = "";
let structuredMode = false;
let requireResume = false;
let resumed = false;
let expectedThreadId = null;
let mappingPath = "";
let schemaPath = "";
let codexHome = "";
let baselineUsage = null;

let child = null;
let childExited = false;
let terminal = false;
let sequence = 0;
let stdoutBytes = 0;
let stdoutBuffer = Buffer.alloc(0);
let sawThread = false;
let sawTurnStart = false;
let sawTurnComplete = false;
let sawTurnFailure = false;
let sawModelError = false;
let threadId = "";
let assistantText = "";
let childUsage = null;
let toolCalls = 0;
let toolResults = 0;
let failedCommandResults = 0;
let startedAt = 0;
let recoveryAttempts = 0;
let awaitingRecoveryThread = false;
const activeTools = new Map();
const toolCounts = {};

function required(name) {
  const value = process.env[name];
  if (typeof value !== "string" || !value) throw new Error(`missing ${name}`);
  return value;
}

function identifier(value, name, pattern = IDENTIFIER) {
  if (value.length > MAX.identifier || !pattern.test(value)) {
    throw new Error(`${name} is invalid`);
  }
  return value;
}

function absolutePath(value, name) {
  if (!value.startsWith("/") || value.includes("\0")) throw new Error(`${name} is invalid`);
  return value;
}

function positiveInteger(value, name, maximum = Number.MAX_SAFE_INTEGER) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw new Error(`${name} is invalid`);
  }
  return parsed;
}

function canonicalBase64(value, maximumBytes, name) {
  if (!value || value.length % 4 !== 0 || !BASE64.test(value)) {
    throw new Error(`${name} is invalid`);
  }
  const bytes = Buffer.from(value, "base64");
  if (!bytes.length || bytes.length > maximumBytes || bytes.toString("base64") !== value) {
    throw new Error(`${name} is invalid`);
  }
  try {
    return fatalUtf8.decode(bytes);
  } catch {
    throw new Error(`${name} is invalid UTF-8`);
  }
}

function parseFlag(name) {
  const value = process.env[name];
  if (value === undefined || value === "") return false;
  if (value !== "1") throw new Error(`${name} must be 1 when enabled`);
  return true;
}

function normalizeStrictOutputSchema(schema) {
  const visit = (node) => {
    if (!node || typeof node !== "object" || Array.isArray(node)) return;

    if (Object.hasOwn(node, "oneOf")) {
      if (!Array.isArray(node.oneOf)) throw new Error("output schema oneOf is invalid");
      if (node.anyOf !== undefined && !Array.isArray(node.anyOf)) {
        throw new Error("output schema anyOf is invalid");
      }
      node.anyOf = [...(node.anyOf ?? []), ...node.oneOf];
      delete node.oneOf;
    }
    if (Object.hasOwn(node, "const")) {
      if (
        node.enum !== undefined &&
        (!Array.isArray(node.enum) || !node.enum.some((value) => isDeepStrictEqual(value, node.const)))
      ) {
        throw new Error("output schema const conflicts with enum");
      }
      node.enum = [node.const];
      delete node.const;
    }
    delete node.discriminator;
    for (const key of STRICT_UNSUPPORTED_SCHEMA_KEYWORDS) delete node[key];

    for (const key of ["$defs", "definitions", "properties"]) {
      const schemas = node[key];
      if (schemas && typeof schemas === "object" && !Array.isArray(schemas)) {
        for (const child of Object.values(schemas)) visit(child);
      }
    }
    for (const key of ["allOf", "anyOf", "prefixItems"]) {
      const schemas = node[key];
      if (Array.isArray(schemas)) for (const child of schemas) visit(child);
    }
    for (const key of [
      "additionalProperties", "items", "not", "if", "then", "else", "contentSchema",
    ]) {
      const child = node[key];
      if (Array.isArray(child)) for (const item of child) visit(item);
      else visit(child);
    }

    const types = Array.isArray(node.type) ? node.type : [node.type];
    if (!types.includes("object")) return;
    const properties = node.properties ?? {};
    if (!properties || typeof properties !== "object" || Array.isArray(properties)) {
      throw new Error("output schema object properties are invalid");
    }
    node.properties = properties;
    node.required = Object.keys(properties);
    node.additionalProperties = false;
  };

  visit(schema);
  return schema;
}

function bounded(value, maximum = MAX.publicTextCharacters) {
  const text = String(value ?? "");
  return text.length <= maximum ? text : `${text.slice(0, maximum)}…[truncated]`;
}

function redact(value) {
  let text = String(value ?? "");
  for (const secret of [virtualKey, prompt, promptBase64]) {
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

function elapsedMs() {
  return startedAt ? Date.now() - startedAt : null;
}

function zeroUsage() {
  return { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
}

function statsWithUsage(usage) {
  return {
    sessionId,
    userMessages: 0,
    assistantMessages: 0,
    toolCalls: 0,
    toolResults: 0,
    totalMessages: 0,
    tokens: {
      ...usage,
      total: usage.input + usage.output + usage.cacheRead + usage.cacheWrite,
    },
    cost: 0,
  };
}

function parseEnvironment() {
  requestId = identifier(required(ENV.requestId), ENV.requestId);
  correlationId = identifier(required(ENV.correlationId), ENV.correlationId);
  sessionId = identifier(required(ENV.sessionId), ENV.sessionId, SESSION_ID);
  promptBase64 = required(ENV.prompt);
  prompt = canonicalBase64(
    promptBase64,
    Buffer.byteLength("x".repeat(MAX.promptCharacters), "utf8") * 4,
    ENV.prompt,
  );
  if (!prompt.trim() || prompt.length > MAX.promptCharacters) throw new Error("prompt is invalid");
  virtualKey = required(ENV.virtualKey);
  if (
    virtualKey.length > MAX.keyCharacters ||
    /[\u0000-\u001f\u007f]/.test(virtualKey)
  ) throw new Error(`${ENV.virtualKey} is invalid`);

  const base = new URL(required(ENV.providerBaseUrl));
  if (
    !["http:", "https:"].includes(base.protocol) ||
    !base.hostname || base.username || base.password || base.search || base.hash ||
    !base.pathname.replace(/\/+$/, "").endsWith("/v1")
  ) throw new Error(`${ENV.providerBaseUrl} is invalid`);
  base.pathname = base.pathname.replace(/\/+$/, "");
  providerBaseUrl = base.toString().replace(/\/$/, "");

  workspace = absolutePath(required(ENV.workspace), ENV.workspace);
  stateDir = absolutePath(required(ENV.stateDir), ENV.stateDir);
  codexBin = absolutePath(required(ENV.codexBin), ENV.codexBin);
  if (!existsSync(workspace) || !statSync(workspace).isDirectory()) {
    throw new Error(`${ENV.workspace} is unavailable`);
  }
  if (!existsSync(codexBin) || !statSync(codexBin).isFile()) {
    throw new Error(`${ENV.codexBin} is unavailable`);
  }
  const version = spawnSync(codexBin, ["--version"], {
    cwd: workspace,
    env: { PATH: process.env.PATH || "" },
    encoding: "utf8",
    timeout: 5_000,
    windowsHide: true,
  });
  if (
    version.error || version.status !== 0 || version.signal != null ||
    String(version.stdout || "").trim() !== CODEX_VERSION
  ) throw new Error("Codex CLI version is unsupported");

  modelRef = required(ENV.modelRef);
  if (!MODEL_REFS.has(modelRef)) throw new Error("Codex requires a GPT runtime profile");
  modelAlias = modelRef.slice("fomo-litellm/".length);
  thinkingLevel = required(ENV.thinking);
  codexThinking = THINKING_MAP.get(thinkingLevel) ?? "";
  if (!codexThinking) throw new Error("Codex thinking level is unsupported");
  contextWindow = positiveInteger(required(ENV.contextWindow), ENV.contextWindow, 250_000);

  requireResume = parseFlag(ENV.requireResume);
  if (parseFlag(ENV.userInput)) throw new Error("Codex does not support FOMO user input");
  const encodedSchema = process.env[ENV.schema] || "";
  if (encodedSchema) {
    schemaText = canonicalBase64(encodedSchema, MAX.schemaBytes, ENV.schema);
    let schema;
    try { schema = JSON.parse(schemaText); } catch { throw new Error("output schema is invalid"); }
    if (!schema || typeof schema !== "object" || Array.isArray(schema) || schema.type !== "object") {
      throw new Error("output schema must describe an object");
    }
    schemaText = JSON.stringify(normalizeStrictOutputSchema(schema));
    if (Buffer.byteLength(schemaText, "utf8") > MAX.schemaBytes) {
      throw new Error("normalized output schema exceeds its limit");
    }
    structuredMode = true;
  }
}

function initializeState() {
  mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  codexHome = join(stateDir, "home");
  const mappings = join(stateDir, "sessions");
  const schemas = join(stateDir, "schemas");
  mkdirSync(codexHome, { recursive: true, mode: 0o700 });
  mkdirSync(mappings, { recursive: true, mode: 0o700 });
  mkdirSync(schemas, { recursive: true, mode: 0o700 });
  const digest = createHash("sha256").update(sessionId).digest("hex");
  mappingPath = join(mappings, `${digest}.json`);
  schemaPath = join(schemas, `${digest}-${requestId}.json`);
  baselineUsage = zeroUsage();

  if (existsSync(mappingPath)) {
    let mapping;
    try { mapping = JSON.parse(readFileSync(mappingPath, "utf8")); } catch {
      throw new Error("Codex session mapping is invalid");
    }
    if (
      !mapping || typeof mapping !== "object" || Array.isArray(mapping) ||
      Object.keys(mapping).some(
        (key) => ![
          "schemaVersion", "threadId", "modelRef", "thinkingLevel", "cumulativeUsage",
        ].includes(key),
      ) ||
      mapping.schemaVersion !== 2 ||
      typeof mapping.threadId !== "string" || !UUID.test(mapping.threadId) ||
      mapping.modelRef !== modelRef || mapping.thinkingLevel !== thinkingLevel
    ) throw new Error("Codex session mapping does not match the run contract");
    baselineUsage = parseStoredUsage(mapping.cumulativeUsage);
    expectedThreadId = mapping.threadId;
    resumed = true;
  } else if (requireResume) {
    throw new Error("session_resume_unavailable");
  }

  if (structuredMode) writeFileSync(schemaPath, `${schemaText}\n`, { mode: 0o600 });
}

function storeThreadMapping(value, cumulativeUsage) {
  const record = `${JSON.stringify({
    schemaVersion: 2,
    threadId: value,
    modelRef,
    thinkingLevel,
    cumulativeUsage,
  })}\n`;
  const temporary = `${mappingPath}.${process.pid}.tmp`;
  let fd = null;
  try {
    fd = openSync(temporary, "wx", 0o600);
    writeFileSync(fd, record, "utf8");
    closeSync(fd);
    fd = null;
    renameSync(temporary, mappingPath);
  } finally {
    if (fd !== null) closeSync(fd);
    rmSync(temporary, { force: true });
  }
}

function codexArguments(forceResume = false) {
  // OpenSandbox is the host isolation boundary. Codex is deliberately
  // unrestricted only inside that disposable generation sandbox; the bypass
  // flag is not used, and approvals are explicitly disabled for headless IO.
  const global = [
    "--model", modelAlias,
    "--sandbox", "danger-full-access",
    "--ask-for-approval", "never",
    "--cd", workspace,
    "--strict-config",
    "-c", 'model_provider="fomo_litellm"',
    "-c", 'model_providers.fomo_litellm.name="FOMO LiteLLM"',
    "-c", `model_providers.fomo_litellm.base_url=${JSON.stringify(providerBaseUrl)}`,
    "-c", 'model_providers.fomo_litellm.env_key="CODEX_API_KEY"',
    "-c", 'model_providers.fomo_litellm.wire_api="responses"',
    "-c", "model_providers.fomo_litellm.supports_websockets=false",
    "-c", "model_providers.fomo_litellm.requires_openai_auth=false",
    "-c", `model_catalog_json=${JSON.stringify(CODEX_MODEL_CATALOG)}`,
    "-c", `model_reasoning_effort=${JSON.stringify(codexThinking)}`,
    "-c", `model_context_window=${contextWindow}`,
    "-c", 'model_reasoning_summary="none"',
    "-c", 'shell_environment_policy.inherit="core"',
    "-c", 'shell_environment_policy.exclude=["CODEX_API_KEY","CODEX_HOME"]',
    "-c", 'shell_environment_policy.set.PNPM_HOME="/opt/fomo/pnpm"',
    "-c", 'shell_environment_policy.set.COREPACK_HOME="/opt/fomo/corepack"',
    "-c", 'shell_environment_policy.set.npm_config_store_dir="/opt/fomo/pnpm/store"',
    "-c", 'shell_environment_policy.set.PLAYWRIGHT_BROWSERS_PATH="/ms-playwright"',
    "-c", 'shell_environment_policy.set.COREPACK_DEFAULT_TO_LATEST="0"',
    "-c", 'shell_environment_policy.set.CI="1"',
    "-c", 'shell_environment_policy.set.PATH="/opt/fomo/pnpm:/opt/fomo/pi/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"',
    "-c", "allow_login_shell=false",
  ];
  const exec = [
    "--json",
    "--skip-git-repo-check",
    "--ignore-user-config",
    "--ignore-rules",
  ];
  if (structuredMode) exec.push("--output-schema", schemaPath);
  if (forceResume || resumed) {
    return [...global, "exec", "resume", ...exec, forceResume ? threadId : expectedThreadId, "-"];
  }
  return [...global, "exec", ...exec, "-"];
}

function spawnCodex(input = prompt, forceResume = false) {
  const environment = { ...process.env };
  for (const name of Object.keys(environment)) {
    if (name.startsWith("FOMO_PI_")) delete environment[name];
  }
  environment.CODEX_HOME = codexHome;
  environment.CODEX_API_KEY = virtualKey;
  environment.NO_COLOR = "1";
  child = spawn(codexBin, codexArguments(forceResume), {
    cwd: workspace,
    env: environment,
    detached: true,
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
  });
  child.stdin.on("error", () => {});
  child.on("error", () => fail("codex_spawn_failed", "Codex could not start.", "booting"));
  child.stdout.on("data", onStdout);
  child.stderr.on("data", () => {
    // Upstream diagnostics can contain provider or repository data. The
    // bridge deliberately does not publish or persist them.
  });
  child.on("close", onClose);
  child.stdin.end(input);
}

function safeToolId(value) {
  const attemptPrefix = recoveryAttempts ? `recovery-${recoveryAttempts}-` : "";
  const raw = typeof value === "string" ? value : "";
  const candidate = `${attemptPrefix}${raw}`;
  if (raw && candidate.length <= MAX.identifier && IDENTIFIER.test(candidate)) {
    return candidate;
  }
  return `${attemptPrefix}codex-${createHash("sha256").update(String(value)).digest("hex").slice(0, 24)}`;
}

function toolKind(item) {
  if (item.type === "command_execution") return "bash";
  if (item.type === "file_change") return "edit";
  if (item.type === "mcp_tool_call") return "mcp";
  if (item.type === "collab_tool_call") return "collab";
  if (item.type === "web_search") return "web_search";
  return null;
}

function toolPath(item) {
  if (!Array.isArray(item.changes)) return null;
  for (const change of item.changes) {
    if (change && typeof change === "object" && typeof change.path === "string") {
      return bounded(redact(change.path), 2_000);
    }
  }
  return null;
}

function beginTool(item) {
  if (structuredMode) return;
  const name = toolKind(item);
  if (!name) return;
  const originalId = String(item.id ?? "");
  if (activeTools.has(originalId)) return;
  const publicId = safeToolId(originalId);
  activeTools.set(originalId, { publicId, name, reportedOutput: false });
  toolCalls += 1;
  toolCounts[name] = (toolCounts[name] || 0) + 1;
  const path = name === "edit" ? toolPath(item) : null;
  emitPi("tool_start", {
    toolCallId: publicId,
    toolName: name,
    args: path ? { path } : {},
    elapsedMs: elapsedMs(),
  });
}

function updateTool(item) {
  if (structuredMode) return;
  beginTool(item);
  const active = activeTools.get(String(item.id ?? ""));
  if (!active) return;
  const output = typeof item.aggregated_output === "string" ? item.aggregated_output : "";
  if (output && !active.reportedOutput) {
    active.reportedOutput = true;
    emitPi("tool_output", {
      toolCallId: active.publicId,
      toolName: active.name,
      text: "Codex tool execution is in progress.",
      cumulative: true,
      elapsedMs: elapsedMs(),
    });
  }
}

function endTool(item) {
  if (structuredMode) return;
  updateTool(item);
  const originalId = String(item.id ?? "");
  const active = activeTools.get(originalId);
  if (!active) return;
  activeTools.delete(originalId);
  toolResults += 1;
  const failed = item.status === "failed" ||
    (Number.isInteger(item.exit_code) && item.exit_code !== 0) ||
    (item.type === "mcp_tool_call" && item.error != null);
  if (failed && item.type === "command_execution") failedCommandResults += 1;
  emitPi("tool_end", {
    toolCallId: active.publicId,
    toolName: active.name,
    isError: failed,
    elapsedMs: elapsedMs(),
  });
}

function parseUsage(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Codex turn usage is missing");
  }
  const integer = (name, optional = false) => {
    const item = value[name];
    if (optional && item === undefined) return 0;
    if (!Number.isSafeInteger(item) || item < 0) throw new Error("Codex turn usage is invalid");
    return item;
  };
  const rawInput = integer("input_tokens");
  const cacheRead = integer("cached_input_tokens", true);
  const cacheWrite = integer("cache_write_input_tokens", true);
  const output = integer("output_tokens");
  if (cacheRead + cacheWrite > rawInput) {
    throw new Error("Codex cached usage exceeds input usage");
  }
  return { input: rawInput - cacheRead - cacheWrite, output, cacheRead, cacheWrite };
}

function parseStoredUsage(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Codex cumulative usage is invalid");
  }
  if (Object.keys(value).some(
    (key) => !["input", "output", "cacheRead", "cacheWrite"].includes(key),
  )) throw new Error("Codex cumulative usage is invalid");
  const result = {};
  for (const name of ["input", "output", "cacheRead", "cacheWrite"]) {
    if (!Number.isSafeInteger(value[name]) || value[name] < 0) {
      throw new Error("Codex cumulative usage is invalid");
    }
    result[name] = value[name];
  }
  return result;
}

function validateCumulativeUsage(current) {
  for (const name of ["input", "output", "cacheRead", "cacheWrite"]) {
    if (current[name] < baselineUsage[name]) {
      throw new Error("Codex cumulative usage moved backwards");
    }
  }
}

function handleItem(event) {
  const item = event.item;
  if (!item || typeof item !== "object" || Array.isArray(item) || typeof item.type !== "string") {
    throw new Error("Codex item is invalid");
  }
  const phase = event.type.slice("item.".length);
  // Codex may publish a private recoverable warning after thread startup but
  // before turn.started (for example, while resolving model metadata).
  if (item.type === "error") {
    if (!sawThread || sawTurnComplete || sawTurnFailure) {
      throw new Error("Codex warning is out of lifecycle order");
    }
    emitPi("inference_heartbeat", { elapsedMs: elapsedMs() });
    return;
  }
  if (!sawTurnStart || sawTurnComplete || sawTurnFailure) {
    throw new Error("Codex item is out of lifecycle order");
  }
  if (item.type === "reasoning" || item.type === "todo_list") {
    emitPi("inference_heartbeat", { elapsedMs: elapsedMs() });
    return;
  }
  if (item.type === "agent_message") {
    if (phase === "completed") {
      if (typeof item.text !== "string" || !item.text.trim()) {
        throw new Error("Codex assistant message is empty");
      }
      assistantText = structuredMode
        ? item.text
        : assistantText ? `${assistantText}\n${item.text}` : item.text;
      if (assistantText.length > MAX.assistantCharacters) {
        throw new Error("Codex assistant message exceeded its limit");
      }
    }
    return;
  }
  if (!toolKind(item)) throw new Error("Codex emitted an unsupported item type");
  if (phase === "started") beginTool(item);
  else if (phase === "updated") updateTool(item);
  else if (phase === "completed") endTool(item);
  else throw new Error("Codex item phase is unsupported");
}

function handleRecord(record) {
  if (!record || typeof record !== "object" || Array.isArray(record) || typeof record.type !== "string") {
    throw new Error("Codex emitted an invalid JSONL record");
  }
  if (awaitingRecoveryThread && record.type !== "thread.started") {
    throw new Error("Codex recovery did not identify its resumed thread");
  }
  if (record.type === "thread.started") {
    if (awaitingRecoveryThread) {
      if (record.thread_id !== threadId) throw new Error("Codex recovery resumed a different thread");
      awaitingRecoveryThread = false;
      return;
    }
    if (sawThread || typeof record.thread_id !== "string" || !UUID.test(record.thread_id)) {
      throw new Error("Codex thread contract is invalid");
    }
    if (expectedThreadId && record.thread_id !== expectedThreadId) {
      throw new Error("Codex resumed a different thread");
    }
    threadId = record.thread_id;
    if (!resumed) storeThreadMapping(threadId, baselineUsage);
    sawThread = true;
    startedAt = Date.now();
    emit("started", {
      sessionId,
      model: modelRef,
      thinkingLevel,
      contextWindow,
      resumed,
      initialStats: statsWithUsage(baselineUsage),
    });
    return;
  }
  if (record.type === "turn.started") {
    if (!sawThread || sawTurnStart || sawTurnComplete) throw new Error("Codex turn start is invalid");
    sawTurnStart = true;
    emitPi("agent_start", { elapsedMs: 0 });
    emitPi("turn_start", { role: "assistant", elapsedMs: 0 });
    return;
  }
  if (record.type.startsWith("item.")) {
    handleItem(record);
    return;
  }
  if (record.type === "turn.completed") {
    if (
      !sawTurnStart || sawTurnComplete || sawTurnFailure ||
      activeTools.size
    ) {
      throw new Error("Codex turn completion is invalid");
    }
    childUsage = parseUsage(record.usage);
    validateCumulativeUsage(childUsage);
    sawTurnComplete = true;
    return;
  }
  if (record.type === "turn.failed") {
    if (!sawTurnStart || sawTurnComplete || sawTurnFailure) {
      throw new Error("Codex turn failure is out of lifecycle order");
    }
    sawTurnFailure = true;
    return;
  }
  // Top-level provider errors may be followed by an internal retry. Keep the
  // body private and wait for the authoritative terminal turn/exit boundary.
  if (record.type === "error") {
    if (sawTurnComplete || sawTurnFailure) {
      throw new Error("Codex provider error is out of lifecycle order");
    }
    sawModelError = true;
    emitPi("inference_heartbeat", { elapsedMs: elapsedMs() });
    return;
  }
  throw new Error("Codex emitted an unsupported event type");
}

function onStdout(chunk) {
  if (terminal) return;
  stdoutBytes += chunk.length;
  if (stdoutBytes > MAX.stdoutBytes) return fail("codex_protocol_failed", "Codex output exceeded its limit.", "running");
  stdoutBuffer = Buffer.concat([stdoutBuffer, chunk]);
  let newline = stdoutBuffer.indexOf(0x0a);
  while (newline >= 0 && !terminal) {
    let line = stdoutBuffer.subarray(0, newline);
    stdoutBuffer = stdoutBuffer.subarray(newline + 1);
    if (line.length && line.at(-1) === 0x0d) line = line.subarray(0, -1);
    if (!line.length || line.length > MAX.lineBytes) {
      fail("codex_protocol_failed", "Codex emitted an invalid JSONL record.", "running");
      return;
    }
    let record;
    try { record = JSON.parse(fatalUtf8.decode(line)); } catch {
      fail("codex_protocol_failed", "Codex emitted invalid JSONL.", "running");
      return;
    }
    try { handleRecord(record); } catch {
      fail("codex_protocol_failed", "Codex violated its runtime contract.", "running");
      return;
    }
    newline = stdoutBuffer.indexOf(0x0a);
  }
  if (stdoutBuffer.length > MAX.lineBytes) {
    fail("codex_protocol_failed", "Codex JSONL record exceeded its limit.", "running");
  }
}

function completedStats() {
  return {
    sessionId,
    userMessages: 1,
    assistantMessages: 1,
    toolCalls,
    toolResults,
    totalMessages: 2 + toolResults,
    tokens: statsWithUsage(childUsage).tokens,
    // LiteLLM's run-scoped max_budget remains the authoritative spend fence;
    // Codex exec JSON currently exposes tokens but no monetary cost.
    cost: 0,
  };
}

function finalizeSuccess() {
  if (terminal) return;
  const publicAssistantText = redact(assistantText);
  // Structured output is a machine contract, not public assistant prose. The
  // synthetic tool lifecycle below must retain the complete object so the
  // control plane can extract it, while the public message lifecycle remains
  // intentionally empty. This matches the Pi/OpenCode adapters and prevents a
  // planning payload from being duplicated into SSE and durable text events.
  const publicLifecycleText = structuredMode ? "" : publicAssistantText;
  let structuredValue = null;
  if (structuredMode) {
    try { structuredValue = JSON.parse(publicAssistantText); } catch {
      return fail("codex_structured_output_invalid", "Codex returned invalid structured output.", "running");
    }
    if (!structuredValue || typeof structuredValue !== "object" || Array.isArray(structuredValue)) {
      return fail("codex_structured_output_invalid", "Codex returned invalid structured output.", "running");
    }
    emitPi("tool_start", {
      toolCallId: "codex-structured-output",
      toolName: STRUCTURED_OUTPUT_TOOL,
      args: structuredValue,
      elapsedMs: elapsedMs(),
    });
    emitPi("tool_end", {
      toolCallId: "codex-structured-output",
      toolName: STRUCTURED_OUTPUT_TOOL,
      isError: false,
      elapsedMs: elapsedMs(),
    });
  }
  // Codex reports thread-cumulative usage. Persist only after the FOMO turn
  // itself is valid so the next resume can expose an exact settlement baseline.
  storeThreadMapping(threadId, childUsage);
  emitPi("message_start", { role: "assistant", elapsedMs: elapsedMs() });
  for (let offset = 0; offset < publicLifecycleText.length; offset += 8_000) {
    emitPi("message_delta", {
      role: "assistant",
      deltaType: "text_delta",
      contentIndex: 0,
      delta: publicLifecycleText.slice(offset, offset + 8_000),
      elapsedMs: elapsedMs(),
    });
  }
  emitPi("message_end", {
    role: "assistant",
    stopReason: "stop",
    elapsedMs: elapsedMs(),
  });
  emitPi("turn_end", {
    role: "assistant",
    text: bounded(publicLifecycleText),
    stopReason: "stop",
    elapsedMs: elapsedMs(),
  });
  emitPi("agent_end", { elapsedMs: elapsedMs() });
  emitPi("agent_settled", { elapsedMs: elapsedMs() });
  const stats = completedStats();
  emit("completed", {
    sessionId,
    state: {
      sessionId,
      messageCount: stats.totalMessages,
      pendingMessageCount: 0,
      isStreaming: false,
      isCompacting: false,
    },
    stats,
    inputRequest: null,
    telemetry: { toolCounts: { ...toolCounts }, lastStopReason: "stop" },
  });
  terminal = true;
  cleanup();
  process.exit(0);
}

function recoverIncompleteCommandFailure() {
  if (
    recoveryAttempts !== 0 || structuredMode || !sawThread ||
    failedCommandResults === 0 || activeTools.size || sawTurnFailure || sawModelError
  ) return false;

  // Codex 0.147 treats a non-zero command exit as a normal tool result and is
  // expected to continue the same turn. Its exec frontend can nevertheless
  // exit cleanly if the in-process event channel closes before the terminal
  // turn notification. Resume the captured UUID once so the model can observe
  // the persisted command result and repair it; never use --last or fallback
  // to a new thread.
  if (sawTurnComplete && childUsage) storeThreadMapping(threadId, childUsage);
  recoveryAttempts += 1;
  awaitingRecoveryThread = true;
  childExited = false;
  sawTurnStart = false;
  sawTurnComplete = false;
  sawTurnFailure = false;
  sawModelError = false;
  assistantText = "";
  childUsage = null;
  emitPi("inference_heartbeat", { elapsedMs: elapsedMs() });
  spawnCodex(COMMAND_FAILURE_RECOVERY_PROMPT, true);
  return true;
}

function onClose(code, signal) {
  childExited = true;
  if (terminal) return;
  if (stdoutBuffer.length) {
    fail("codex_protocol_failed", "Codex ended with an incomplete JSONL record.", "running");
    return;
  }
  if (sawTurnFailure || (!sawTurnComplete && sawModelError)) {
    fail(
      "codex_model_failed",
      "Codex could not complete the model turn.",
      sawThread ? "running" : "booting",
    );
    return;
  }
  if (
    code === 0 && signal === null &&
    (!sawTurnComplete || !assistantText.trim()) &&
    recoverIncompleteCommandFailure()
  ) return;
  if (
    code === 0 && signal === null &&
    (!sawThread || !sawTurnComplete || !childUsage || !assistantText.trim())
  ) {
    fail("codex_protocol_failed", "Codex ended without a valid terminal turn.", sawThread ? "running" : "booting");
    return;
  }
  if (code !== 0 || signal !== null || !sawThread || !sawTurnComplete || !childUsage) {
    fail("codex_runtime_failed", "Codex runtime could not complete the request.", sawThread ? "running" : "booting");
    return;
  }
  finalizeSuccess();
}

function killChildGroup(signal) {
  if (!child?.pid || childExited) return;
  try { process.kill(-child.pid, signal); } catch { /* best effort */ }
}

function cleanup() {
  if (schemaPath) rmSync(schemaPath, { force: true });
}

function fail(code, message, phase) {
  if (terminal) return;
  terminal = true;
  emit("failed", { code, message, phase });
  killChildGroup("SIGTERM");
  cleanup();
  setTimeout(() => {
    killChildGroup("SIGKILL");
    process.exit(1);
  }, child && !childExited ? 250 : 0);
}

function main() {
  try {
    parseEnvironment();
    initializeState();
    spawnCodex();
  } catch (error) {
    const unavailable = error instanceof Error && error.message === "session_resume_unavailable";
    fail(
      unavailable ? "session_resume_unavailable" : "codex_invalid_environment",
      unavailable ? "The persisted Codex session is unavailable." : "Codex runtime configuration is invalid.",
      "booting",
    );
  }
}

process.on("SIGTERM", () => {
  killChildGroup("SIGTERM");
  cleanup();
  process.exit(143);
});
process.on("SIGHUP", () => {
  killChildGroup("SIGHUP");
  cleanup();
  process.exit(129);
});
process.on("uncaughtException", () => fail("codex_bridge_failed", "Codex bridge failed.", "internal"));
process.on("unhandledRejection", () => fail("codex_bridge_failed", "Codex bridge failed.", "internal"));

main();
