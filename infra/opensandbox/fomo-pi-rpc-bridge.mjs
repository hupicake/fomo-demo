#!/usr/bin/env node
/**
 * Trusted foreground bridge between OpenSandbox and Pi RPC.
 *
 * The bridge owns Pi stdin/stdout for one turn. A later repair turn starts a
 * new bridge process with the same session id; the session is only a cache.
 * stdout is reserved for the versioned FOMO JSONL protocol. Secrets and Pi
 * stderr never enter that protocol.
 */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { TextDecoder } from "node:util";

const SCHEMA_VERSION = 1;
const PROVIDER_ID = "fomo-litellm";
const MODEL_ID = "fomo-pi-flash";
const MODEL_REF = `${PROVIDER_ID}/${MODEL_ID}`;
const THINKING_LEVEL = "max";
const ALLOWED_TOOLS = "read,grep,find,ls,edit,write";

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
let privateAgentDir = null;
let shuttingDown = false;

const pendingResponses = new Map();
const initial = { state: null, stats: null };
const final = { state: null, stats: null };
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
  if (!existsSync(workspace) || !statSync(workspace).isDirectory()) throw new Error(`${ENV.workspace} is not a directory`);
  if (!existsSync(piBin) || !statSync(piBin).isFile()) throw new Error(`${ENV.piBin} is not a file`);

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
        compat: {
          supportsDeveloperRole: false,
          supportsReasoningEffort: true,
          requiresReasoningContentOnAssistantMessages: true,
          thinkingFormat: "deepseek",
          maxTokensField: "max_tokens",
        },
        models: [{
          id: MODEL_ID,
          name: MODEL_ID,
          reasoning: true,
          input: ["text"],
          contextWindow: 1_000_000,
          maxTokens: 32_768,
          thinkingLevelMap: {
            off: null,
            minimal: null,
            low: null,
            medium: null,
            high: null,
            xhigh: null,
            max: "max",
          },
        }],
      },
    },
  };
  writeFileSync(join(privateAgentDir, "models.json"), `${JSON.stringify(models, null, 2)}\n`, { mode: 0o600 });
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

  child = spawn(piBin, [
    "--mode", "rpc",
    "--session-id", sessionId,
    "--session-dir", sessionsDir,
    "--model", MODEL_REF,
    "--thinking", THINKING_LEVEL,
    "--tools", ALLOWED_TOOLS,
    "--no-context-files",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-approve",
    "--offline",
  ], {
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

function send(id, type) {
  if (!child || child.stdin.destroyed || child.stdin.writableEnded) {
    fail("stdin_closed", "Pi stdin is not writable", 1);
    return;
  }
  const message = { id, type };
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
  if (message.id === "initial-state") initial.state = summarizeState(message.data);
  else if (message.id === "initial-stats") initial.stats = summarizeStats(message.data);
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
  if (data.model.provider !== PROVIDER_ID || data.model.id !== MODEL_ID || data.thinkingLevel !== THINKING_LEVEL) {
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
  if (lifecycle !== "booting" || !initial.state || !initial.stats) return;
  lifecycle = "running";
  emit("started", {
    sessionId,
    model: MODEL_REF,
    thinkingLevel: THINKING_LEVEL,
    resumed: initial.state.messageCount > 0,
    initialStats: initial.stats,
  });
  send("prompt", "prompt");
}

function onSettled() {
  if (lifecycle !== "running" || !promptAccepted) {
    fail("invalid_lifecycle", "agent_settled arrived before prompt acceptance or while not running", 1);
    return;
  }
  lifecycle = "settled";
  send("final-state", "get_state");
  send("final-stats", "get_session_stats");
}

function maybeFinalize() {
  if (lifecycle !== "settled" || !final.state || !final.stats) return;
  lifecycle = "finalizing";
  if (timeoutHandle) clearTimeout(timeoutHandle);
  child.stdin.end();
}

function maybeComplete() {
  if (lifecycle !== "finalizing" || !stdoutEnded || !childExited) return;
  if (childExitCode !== 0 || childExitSignal !== null || pendingResponses.size) {
    fail("invalid_exit", "Pi did not exit cleanly after final responses", 1);
    return;
  }
  lifecycle = "completed";
  emit("completed", { sessionId, state: final.state, stats: final.stats });
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

function emitPi(kind, payload = {}) {
  if (!PUBLIC_PI_KINDS.has(kind)) {
    fail("unknown_public_event", `bridge attempted unknown public Pi event ${kind}`, 1);
    return;
  }
  emit("pi.event", { kind, ...payload });
}

function handleEvent(message) {
  if (lifecycle !== "running") {
    fail("invalid_lifecycle", `Pi event ${message.type} arrived while ${lifecycle}`, 1);
    return;
  }
  switch (message.type) {
    case "agent_start": emitPi("agent_start"); break;
    case "agent_end": emitPi("agent_end", { willRetry: message.willRetry === true }); break;
    case "agent_settled": emitPi("agent_settled"); onSettled(); break;
    case "turn_start": emitPi("turn_start"); break;
    case "turn_end": emitPi("turn_end", {
      role: String(message.message?.role ?? ""),
      stopReason: String(message.message?.stopReason ?? ""),
      text: bounded(redact(publicTextBlocks(message.message?.content)), LIMITS.publicTextCharacters),
      toolResults: Array.isArray(message.toolResults) ? message.toolResults.slice(0, LIMITS.arrayItems).map((result) => ({
        toolCallId: String(result?.toolCallId ?? ""),
        toolName: String(result?.toolName ?? ""),
        isError: result?.isError === true,
      })) : [],
    }); break;
    case "message_start": emitPi("message_start", { role: String(message.message?.role ?? "") }); break;
    case "message_end": emitPi("message_end", {
      role: String(message.message?.role ?? ""),
      stopReason: String(message.message?.stopReason ?? ""),
    }); break;
    case "message_update": handleMessageDelta(message.assistantMessageEvent); break;
    case "bash_execution_update": emitPi("bash_output", {
      delta: bounded(redact(message.delta), LIMITS.publicDeltaCharacters),
    }); break;
    case "tool_execution_start": emitPi("tool_start", {
      toolCallId: String(message.toolCallId ?? ""),
      toolName: String(message.toolName ?? ""),
      args: sanitizePublic(message.args),
    }); break;
    case "tool_execution_update": emitPi("tool_output", {
      toolCallId: String(message.toolCallId ?? ""),
      toolName: String(message.toolName ?? ""),
      text: bounded(redact(publicTextBlocks(message.partialResult?.content)), LIMITS.publicTextCharacters),
      cumulative: true,
    }); break;
    case "tool_execution_end": emitPi("tool_end", {
      toolCallId: String(message.toolCallId ?? ""),
      toolName: String(message.toolName ?? ""),
      isError: message.isError === true,
    }); break;
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

async function shutdown(exitCode) {
  if (shuttingDown) return;
  shuttingDown = true;
  if (timeoutHandle) clearTimeout(timeoutHandle);
  if (child && !childExited) {
    try { child.stdin.write(`${JSON.stringify({ id: "abort", type: "abort" })}\n`); } catch { /* continue */ }
    await new Promise((resolve) => setTimeout(resolve, graceSeconds * 1000));
    if (!childExited) {
      killProcessGroup("SIGTERM");
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (!childExited) killProcessGroup("SIGKILL");
    }
  }
  cleanPrivateConfiguration();
  process.exit(exitCode);
}

function fail(code, message, exitCode) {
  if (lifecycle === "failed" || lifecycle === "completed") return;
  const phase = lifecycle;
  lifecycle = "failed";
  emit("failed", { code, message: bounded(redact(message), 4096), phase });
  void shutdown(exitCode);
}

function main() {
  try {
    parseEnvironment();
    const sessionsDir = writePrivateConfiguration();
    lifecycle = "booting";
    spawnPi(sessionsDir);
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
