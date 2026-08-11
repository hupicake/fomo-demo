import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { chmod, mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const BRIDGE = new URL("./fomo-opencode-rpc-bridge.mjs", import.meta.url);

const FAKE_SDK = String.raw`
import { appendFileSync } from "node:fs";

let messageQueryCount = 0;
let activeSessionID = "";
let workspaceMessageID = "";
let workspaceBusy = false;
let workspaceComplete = false;
let workspaceError = null;
const eventQueue = [];
const eventWaiters = [];

const permissions = {
  planner: [
    ["read", "deny"], ["edit", "deny"], ["glob", "deny"], ["grep", "deny"],
    ["list", "deny"], ["bash", "deny"], ["todowrite", "deny"], ["task", "deny"],
    ["question", "deny"], ["skill", "deny"], ["webfetch", "deny"], ["websearch", "deny"],
    ["external_directory", "deny"], ["doom_loop", "deny"],
  ],
  workspace: [
    ["read", "allow"], ["edit", "allow"], ["glob", "allow"], ["grep", "allow"],
    ["list", "allow"], ["bash", "allow"], ["todowrite", "allow"], ["task", "deny"],
    ["question", "deny"], ["skill", "deny"], ["webfetch", "deny"], ["websearch", "deny"],
    ["external_directory", "deny"], ["doom_loop", "deny"],
  ],
};

function permissionProfile(stage) {
  return permissions[stage].map(([permission, action]) => ({ permission, pattern: "*", action }));
}

function stageFromSessionID(sessionID) {
  if (sessionID === "oc-planner-1") return "planner";
  if (sessionID === "oc-workspace-1") return "workspace";
  throw new Error("unknown fake session");
}

function sessionRecord(sessionID) {
  const stage = stageFromSessionID(sessionID);
  return {
    id: sessionID,
    metadata: { fomoSessionStage: stage, fomoSessionPolicyVersion: 1 },
    permission: process.env.FOMO_TEST_INVALID_SESSION_PERMISSION === "1"
      ? []
      : permissionProfile(stage),
  };
}

function assistant({
  id = "assistant-1",
  parentID = "user-1",
  completed = true,
  error = undefined,
} = {}) {
  return {
    id,
    sessionID: activeSessionID,
    role: "assistant",
    time: { created: 2, ...(completed ? { completed: 3 } : {}) },
    ...(error ? { error } : {}),
    parentID,
    modelID: "fomo-pi-deepseek-flash",
    providerID: "fomo-litellm",
    mode: "build",
    agent: "build",
    path: { cwd: "/workspace", root: "/workspace" },
    cost: 0.001,
    tokens: { input: 10, output: 5, reasoning: 0, cache: { read: 0, write: 0 } },
    finish: "stop",
  };
}

function user(id = "user-1") {
  return {
    id,
    sessionID: activeSessionID,
    role: "user",
    time: { created: 1 },
    agent: "build",
    model: { providerID: "fomo-litellm", modelID: "fomo-pi-deepseek-flash" },
  };
}

function pushEvent(event) {
  const waiter = eventWaiters.shift();
  if (waiter) waiter(event);
  else eventQueue.push(event);
}

async function nextEvent(signal) {
  if (eventQueue.length) return eventQueue.shift();
  return new Promise((resolve) => {
    const done = (value) => {
      signal.removeEventListener("abort", aborted);
      resolve(value);
    };
    const aborted = () => done(null);
    eventWaiters.push(done);
    signal.addEventListener("abort", aborted, { once: true });
  });
}

function workspaceAssistant() {
  return assistant({
    id: "assistant-workspace-1",
    parentID: workspaceMessageID,
    completed: workspaceComplete,
    error: workspaceError,
  });
}

function workspaceParts() {
  return [
    {
      id: "part-tool-1",
      sessionID: activeSessionID,
      messageID: "assistant-workspace-1",
      type: "tool",
      callID: "tool-1",
      tool: "apply_patch",
      state: {
        status: "completed",
        input: { patch: "safe fake patch" },
        output: "patched",
        title: "patch",
        metadata: {},
        time: { start: 2, end: 3 },
      },
    },
    {
      id: "part-text-1",
      sessionID: activeSessionID,
      messageID: "assistant-workspace-1",
      type: "text",
      text: "done",
      time: { start: 2, end: 3 },
    },
    {
      id: "part-step-1",
      sessionID: activeSessionID,
      messageID: "assistant-workspace-1",
      type: "step-finish",
      reason: "stop",
      cost: 0.001,
      tokens: { input: 10, output: 5, reasoning: 0, cache: { read: 0, write: 0 } },
    },
  ];
}

function trace(kind, value = {}) {
  if (!process.env.FOMO_TEST_TRACE_FILE) return;
  appendFileSync(process.env.FOMO_TEST_TRACE_FILE, JSON.stringify({ kind, ...value }) + "\n");
}

export async function createOpencodeServer(options) {
  if (process.env.FOMO_TEST_RUNTIME_FAILURE === "1") {
    throw new Error("OpenCode server leaked password=runtime-private-value");
  }
  if (options.hostname !== "127.0.0.1" || options.port !== 0) throw new Error("not loopback");
  if (process.env.OPENCODE_DISABLE_PROJECT_CONFIG !== "1" || process.env.OPENCODE_PURE !== "1") {
    throw new Error("unsafe OpenCode environment");
  }
  if (options.config.plugin.length !== 0 || Object.keys(options.config.mcp).length !== 0) {
    throw new Error("plugins or MCP were enabled");
  }
  if (options.config.provider["fomo-litellm"].options.apiKey !== "sk-run-secret") {
    throw new Error("missing run-scoped key");
  }
  return { url: "http://127.0.0.1:4096", close() {} };
}

export function createOpencodeClient() {
  return {
    tool: {
      async list(parameters) {
        trace("tool.list", parameters);
        if (parameters.provider !== "fomo-litellm" || parameters.model !== "fomo-pi-deepseek-flash") {
          throw new Error("wrong tool registry model");
        }
        const ids = ["read", "glob", "grep", "list", "bash", "todowrite"];
        if (process.env.FOMO_TEST_MISSING_MUTATE_TOOL !== "1") ids.push("apply_patch");
        return { data: ids.map((id) => ({ id, description: id, parameters: {} })) };
      },
    },
    event: {
      async subscribe(_parameters, options) {
        return {
          stream: (async function* () {
            yield { type: "server.connected", properties: {} };
            while (!options.signal.aborted) {
              const event = await nextEvent(options.signal);
              if (!event) break;
              yield event;
            }
          })(),
        };
      },
    },
    session: {
      async create(parameters) {
        if (parameters.model.providerID !== "fomo-litellm") throw new Error("wrong provider");
        const stage = parameters.metadata?.fomoSessionStage;
        if (!permissions[stage]) throw new Error("missing stage metadata");
        if (parameters.metadata.fomoSessionPolicyVersion !== 1) throw new Error("wrong policy version");
        if (JSON.stringify(parameters.permission) !== JSON.stringify(permissionProfile(stage))) {
          throw new Error("wrong stage permission profile");
        }
        activeSessionID = "oc-" + stage + "-1";
        trace("session.create", { sessionID: activeSessionID, stage, permission: parameters.permission });
        return { data: sessionRecord(activeSessionID) };
      },
      async get(parameters) {
        activeSessionID = parameters.sessionID;
        trace("session.get", { sessionID: activeSessionID });
        return { data: sessionRecord(activeSessionID) };
      },
      async messages() {
        messageQueryCount += 1;
        if (process.env.FOMO_TEST_INITIAL_MESSAGES_FAILURE === "1" && messageQueryCount === 1) {
          return { error: { message: "resume leaked api_key=resume-private-value" } };
        }
        if (messageQueryCount === 1 && process.env.FOMO_TEST_HAS_HISTORY !== "1") return { data: [] };
        if (process.env.FOMO_TEST_FINAL_MESSAGES_FAILURE === "1") {
          return { error: { message: "history leaked api_key=history-private-value" } };
        }
        if (workspaceMessageID) {
          return {
            data: [
              { info: user(workspaceMessageID), parts: [] },
              { info: workspaceAssistant(), parts: workspaceComplete && !workspaceError ? workspaceParts() : [] },
            ],
          };
        }
        const info = process.env.FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64
          ? { ...assistant(), structured: { answer: "ok" } }
          : assistant();
        return { data: [{ info: user(), parts: [] }, { info, parts: [{ type: "text", text: "done" }] }] };
      },
      async status() {
        return { data: workspaceBusy ? { [activeSessionID]: { type: "busy" } } : {} };
      },
      async prompt(parameters) {
        if (parameters.model.providerID !== "fomo-litellm") throw new Error("wrong prompt provider");
        if (Object.hasOwn(parameters, "tools")) throw new Error("deprecated prompt tools supplied");
        if (parameters.sessionID !== activeSessionID) throw new Error("wrong active session");
        trace("session.prompt", {
          sessionID: parameters.sessionID,
          structured: parameters.format?.type === "json_schema",
          hasTools: Object.hasOwn(parameters, "tools"),
        });
        // Real SDK prompt completion follows its streamed tool terminal events.
        await new Promise((resolve) => setTimeout(resolve, 5));
        if (process.env.FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64) {
          if (stageFromSessionID(parameters.sessionID) !== "planner") throw new Error("wrong planning session");
          if (parameters.format?.type !== "json_schema" || parameters.format.retryCount !== 2) {
            throw new Error("missing structured format");
          }
          return { data: { info: { ...assistant(), structured: { answer: "ok" } }, parts: [] } };
        }
        throw new Error("workspace must use promptAsync");
      },
      async promptAsync(parameters) {
        if (stageFromSessionID(parameters.sessionID) !== "workspace") throw new Error("wrong workspace session");
        if (!parameters.messageID?.startsWith("msg")) throw new Error("missing deterministic message id");
        if (Object.hasOwn(parameters, "tools")) throw new Error("deprecated prompt tools supplied");
        workspaceMessageID = parameters.messageID;
        workspaceBusy = true;
        workspaceComplete = false;
        workspaceError = null;
        trace("session.promptAsync", {
          sessionID: parameters.sessionID,
          messageID: parameters.messageID,
          hasTools: Object.hasOwn(parameters, "tools"),
        });
        pushEvent({
          type: "message.updated",
          properties: { sessionID: activeSessionID, info: user(workspaceMessageID) },
        });
        pushEvent({
          type: "session.status",
          properties: { sessionID: activeSessionID, status: { type: "busy" } },
        });
        pushEvent({
          type: "message.updated",
          properties: { sessionID: activeSessionID, info: workspaceAssistant() },
        });
        pushEvent({
          type: "message.part.updated",
          properties: {
            sessionID: activeSessionID,
            part: {
              id: "part-step-start-1", sessionID: activeSessionID,
              messageID: "assistant-workspace-1", type: "step-start",
            },
          },
        });
        setTimeout(() => {
          if (process.env.FOMO_TEST_MODEL_FAILURE === "1") {
            workspaceError = {
              name: "APIError",
              data: { message: "provider leaked api_key=model-private-value", isRetryable: false },
            };
            workspaceComplete = true;
            workspaceBusy = false;
            pushEvent({
              type: "message.updated",
              properties: { sessionID: activeSessionID, info: workspaceAssistant() },
            });
            pushEvent({
              type: "session.error",
              properties: { sessionID: activeSessionID, error: workspaceError },
            });
            pushEvent({
              type: "session.status",
              properties: { sessionID: activeSessionID, status: { type: "idle" } },
            });
            return;
          }
          const [tool, text, step] = workspaceParts();
          pushEvent({
            type: "message.part.updated",
            properties: {
              sessionID: activeSessionID,
              part: { ...tool, state: { ...tool.state, status: "running", time: { start: 2 } } },
            },
          });
          pushEvent({ type: "message.part.updated", properties: { sessionID: activeSessionID, part: tool } });
          pushEvent({
            type: "message.part.updated",
            properties: {
              sessionID: activeSessionID,
              part: { ...text, text: "", time: { start: 2 } },
            },
          });
          pushEvent({
            type: "message.part.delta",
            properties: {
              sessionID: activeSessionID, messageID: text.messageID,
              partID: text.id, field: "text", delta: "done",
            },
          });
          pushEvent({ type: "message.part.updated", properties: { sessionID: activeSessionID, part: text } });
          pushEvent({ type: "message.part.updated", properties: { sessionID: activeSessionID, part: step } });
          workspaceComplete = true;
          workspaceBusy = false;
          pushEvent({
            type: "message.updated",
            properties: { sessionID: activeSessionID, info: workspaceAssistant() },
          });
          pushEvent({
            type: "session.status",
            properties: { sessionID: activeSessionID, status: { type: "idle" } },
          });
        }, 10);
        return { data: undefined, response: { status: 204 } };
      },
      async abort() { return { data: true }; },
    },
  };
}
`;

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "fomo-opencode-bridge-"));
  const workspace = join(root, "workspace");
  const state = join(root, "state");
  const bin = join(root, "opencode");
  const sdk = join(root, "fake-sdk.mjs");
  await mkdir(workspace);
  await writeFile(bin, "#!/bin/sh\nexit 0\n");
  await chmod(bin, 0o755);
  await writeFile(sdk, FAKE_SDK);
  return { root, workspace, state, bin, sdk };
}

function baseEnvironment(paths) {
  return {
    ...process.env,
    NODE_ENV: "test",
    FOMO_OPENCODE_SDK_PATH: paths.sdk,
    FOMO_PI_PROMPT_B64: Buffer.from("private prompt").toString("base64"),
    FOMO_PI_SESSION_ID: "session-1",
    FOMO_PI_REQUEST_ID: "request-1",
    FOMO_PI_CORRELATION_ID: "run-1",
    FOMO_PI_PROVIDER_BASE_URL: "http://litellm:4000/v1",
    FOMO_PI_VIRTUAL_KEY: "sk-run-secret",
    FOMO_PI_WORKSPACE: paths.workspace,
    FOMO_PI_STATE_DIR: paths.state,
    FOMO_PI_BIN: paths.bin,
    FOMO_PI_THINKING_LEVEL: "high",
    FOMO_PI_MODEL_REF: "fomo-litellm/fomo-pi-deepseek-flash",
    FOMO_PI_CONTEXT_WINDOW: "1000000",
    FOMO_PI_GRACE_SECONDS: "1",
  };
}

async function runBridge(environment) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [BRIDGE.pathname], { env: environment });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`bridge test timed out\nstdout=${stdout}\nstderr=${stderr}`));
    }, 10_000);
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("exit", (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout, stderr });
    });
  });
}

function envelopes(stdout) {
  return stdout.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

test("OpenCode bridge emits compatible public text lifecycle without leaking secrets", async () => {
  const paths = await fixture();
  const result = await runBridge(baseEnvironment(paths));
  assert.equal(result.code, 0, `${result.stderr}\n${result.stdout}`);
  assert.equal(result.signal, null);
  assert.doesNotMatch(result.stdout, /private prompt|sk-run-secret/);
  assert.doesNotMatch(result.stderr, /private prompt|sk-run-secret/);

  const records = envelopes(result.stdout);
  assert.deepEqual(records.map((record) => record.seq), records.map((_, index) => index + 1));
  assert.equal(records[0].type, "started");
  assert.equal(records[0].payload.model, "fomo-litellm/fomo-pi-deepseek-flash");
  assert.deepEqual(records[0].payload.capabilities, {
    structuredOutput: false,
    repoRead: true,
    repoMutate: true,
    commandExec: true,
    sessionResume: true,
    sessionCancel: true,
  });
  assert.ok(records.some((record) =>
    record.type === "pi.event" && record.payload.kind === "message_delta" && record.payload.delta === "done"));
  assert.equal(records.at(-1).type, "completed", JSON.stringify(records.at(-1)));
  assert.equal(records.at(-2).payload.kind, "agent_settled");
  assert.equal(records.at(-1).payload.stats.tokens.total, 15);

  const mapping = JSON.parse(await readFile(join(paths.state, "session-map", "session-1.json"), "utf8"));
  assert.deepEqual(mapping, {
    schemaVersion: 2,
    policyVersion: 1,
    fomoSessionId: "session-1",
    plannerSessionId: null,
    workspaceSessionId: "oc-workspace-1",
  });
});

test("OpenCode bridge exposes SDK JSON Schema result as the existing terminating tool", async () => {
  const paths = await fixture();
  const schema = { type: "object", properties: { answer: { type: "string" } }, required: ["answer"] };
  const environment = {
    ...baseEnvironment(paths),
    FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64: Buffer.from(JSON.stringify(schema)).toString("base64"),
  };
  const result = await runBridge(environment);
  assert.equal(result.code, 0, result.stderr);
  const records = envelopes(result.stdout);
  assert.deepEqual(records[0].payload.capabilities, {
    structuredOutput: true,
    repoRead: false,
    repoMutate: false,
    commandExec: false,
    sessionResume: true,
    sessionCancel: true,
  });
  const toolStart = records.find((record) =>
    record.type === "pi.event" && record.payload.kind === "tool_start");
  const toolEnd = records.find((record) =>
    record.type === "pi.event" && record.payload.kind === "tool_end");
  assert.equal(toolStart.payload.toolName, "submit_structured_output");
  assert.deepEqual(toolStart.payload.args, { answer: "ok" });
  assert.equal(toolEnd.payload.toolCallId, toolStart.payload.toolCallId);
  assert.equal(toolEnd.payload.isError, false);
  assert.equal(records.at(-2).payload.kind, "agent_settled");
  assert.equal(records.at(-1).type, "completed");
});

test("OpenCode bridge keeps a successful structured result when final telemetry is unavailable", async () => {
  const paths = await fixture();
  const schema = { type: "object", properties: { answer: { type: "string" } }, required: ["answer"] };
  const result = await runBridge({
    ...baseEnvironment(paths),
    FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64: Buffer.from(JSON.stringify(schema)).toString("base64"),
    FOMO_TEST_FINAL_MESSAGES_FAILURE: "1",
  });

  assert.equal(result.code, 0, result.stderr);
  assert.doesNotMatch(result.stdout, /history-private-value|api_key/);
  assert.doesNotMatch(result.stderr, /history-private-value|api_key/);
  const records = envelopes(result.stdout);
  assert.equal(records.at(-2).payload.kind, "agent_settled");
  assert.equal(records.at(-1).type, "completed");
  assert.equal(records.at(-1).payload.stats.userMessages, 1);
  assert.equal(records.at(-1).payload.stats.assistantMessages, 1);
});

test("OpenCode bridge limits unavailable history fallback to the structured planner session", async () => {
  const paths = await fixture();
  const schema = { type: "object", properties: { answer: { type: "string" } }, required: ["answer"] };
  const structuredEnvironment = {
    ...baseEnvironment(paths),
    FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64: Buffer.from(JSON.stringify(schema)).toString("base64"),
  };
  const first = await runBridge(structuredEnvironment);
  assert.equal(first.code, 0, first.stderr);

  const resumed = await runBridge({
    ...structuredEnvironment,
    FOMO_PI_REQUIRE_RESUME: "1",
    FOMO_TEST_INITIAL_MESSAGES_FAILURE: "1",
  });

  assert.equal(resumed.code, 0, resumed.stderr);
  assert.doesNotMatch(resumed.stdout, /resume-private-value|api_key/);
  assert.doesNotMatch(resumed.stderr, /resume-private-value|api_key/);
  const records = envelopes(resumed.stdout);
  assert.equal(records[0].type, "started");
  assert.equal(records[0].payload.resumed, true);
  assert.equal(records.at(-1).type, "completed");
});

test("OpenCode bridge fails closed when workspace repair history is unreadable", async () => {
  const paths = await fixture();
  const first = await runBridge(baseEnvironment(paths));
  assert.equal(first.code, 0, first.stderr);

  const resumed = await runBridge({
    ...baseEnvironment(paths),
    FOMO_PI_REQUIRE_RESUME: "1",
    FOMO_TEST_INITIAL_MESSAGES_FAILURE: "1",
  });

  assert.equal(resumed.code, 1, resumed.stderr);
  assert.doesNotMatch(resumed.stdout, /resume-private-value|api_key/);
  const records = envelopes(resumed.stdout);
  assert.equal(records.some((record) => record.type === "started"), false);
  assert.equal(records.at(-1).type, "failed");
  assert.equal(records.at(-1).payload.code, "opencode_runtime_failed");
});

test("structured planning cannot poison the isolated workspace session and workspace resume", async () => {
  const paths = await fixture();
  const traceFile = join(paths.root, "sdk-trace.jsonl");
  await writeFile(traceFile, "");
  const schema = { type: "object", properties: { answer: { type: "string" } }, required: ["answer"] };

  const planning = await runBridge({
    ...baseEnvironment(paths),
    FOMO_TEST_TRACE_FILE: traceFile,
    FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64: Buffer.from(JSON.stringify(schema)).toString("base64"),
  });
  assert.equal(planning.code, 0, planning.stderr);

  const workspace = await runBridge({
    ...baseEnvironment(paths),
    FOMO_TEST_TRACE_FILE: traceFile,
  });
  assert.equal(workspace.code, 0, workspace.stderr);
  const workspaceRecords = envelopes(workspace.stdout);
  assert.deepEqual(workspaceRecords[0].payload.capabilities, {
    structuredOutput: false,
    repoRead: true,
    repoMutate: true,
    commandExec: true,
    sessionResume: true,
    sessionCancel: true,
  });
  assert.equal(workspaceRecords.at(-1).payload.telemetry.toolCounts.apply_patch, 1);
  assert.equal(typeof workspaceRecords.at(-1).payload.telemetry.firstEditOrWriteToolElapsedMs, "number");

  const resumed = await runBridge({
    ...baseEnvironment(paths),
    FOMO_TEST_TRACE_FILE: traceFile,
    FOMO_TEST_HAS_HISTORY: "1",
    FOMO_PI_REQUIRE_RESUME: "1",
  });
  assert.equal(resumed.code, 0, resumed.stderr);
  assert.equal(envelopes(resumed.stdout)[0].payload.resumed, true);

  const mapping = JSON.parse(await readFile(join(paths.state, "session-map", "session-1.json"), "utf8"));
  assert.deepEqual(mapping, {
    schemaVersion: 2,
    policyVersion: 1,
    fomoSessionId: "session-1",
    plannerSessionId: "oc-planner-1",
    workspaceSessionId: "oc-workspace-1",
  });

  const trace = (await readFile(traceFile, "utf8"))
    .trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
  assert.deepEqual(
    trace.filter((entry) => entry.kind === "session.create").map((entry) => [entry.stage, entry.sessionID]),
    [["planner", "oc-planner-1"], ["workspace", "oc-workspace-1"]],
  );
  assert.deepEqual(
    trace.filter((entry) => entry.kind === "session.prompt")
      .map((entry) => [entry.sessionID, entry.structured, entry.hasTools]),
    [["oc-planner-1", true, false]],
  );
  assert.deepEqual(
    trace.filter((entry) => entry.kind === "session.promptAsync")
      .map((entry) => [entry.sessionID, entry.hasTools]),
    [["oc-workspace-1", false], ["oc-workspace-1", false]],
  );
});

test("OpenCode bridge fails closed before prompting when required tools are unavailable", async () => {
  const paths = await fixture();
  const traceFile = join(paths.root, "sdk-trace.jsonl");
  await writeFile(traceFile, "");
  const result = await runBridge({
    ...baseEnvironment(paths),
    FOMO_TEST_TRACE_FILE: traceFile,
    FOMO_TEST_MISSING_MUTATE_TOOL: "1",
  });

  assert.equal(result.code, 1, result.stderr);
  const records = envelopes(result.stdout);
  assert.equal(records.at(-1).type, "failed");
  assert.equal(records.at(-1).payload.code, "opencode_capability_unavailable");
  const trace = (await readFile(traceFile, "utf8"))
    .trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
  assert.ok(trace.some((entry) => entry.kind === "tool.list"));
  assert.ok(!trace.some((entry) => ["session.prompt", "session.promptAsync"].includes(entry.kind)));
});

test("OpenCode bridge fails closed when stage session permissions are not effective", async () => {
  const paths = await fixture();
  const result = await runBridge({
    ...baseEnvironment(paths),
    FOMO_TEST_INVALID_SESSION_PERMISSION: "1",
  });

  assert.equal(result.code, 1, result.stderr);
  const failure = envelopes(result.stdout).at(-1);
  assert.equal(failure.type, "failed");
  assert.equal(failure.payload.code, "opencode_capability_unavailable");
  assert.equal(failure.payload.phase, "booting");
});

test("OpenCode bridge rejects legacy shared-session mappings before prompting", async () => {
  const paths = await fixture();
  await mkdir(join(paths.state, "session-map"), { recursive: true });
  await writeFile(join(paths.state, "session-map", "session-1.json"), JSON.stringify({
    schemaVersion: 1,
    fomoSessionId: "session-1",
    openCodeSessionId: "oc-session-legacy",
  }));
  const result = await runBridge(baseEnvironment(paths));

  assert.equal(result.code, 1, result.stderr);
  const failure = envelopes(result.stdout).at(-1);
  assert.equal(failure.type, "failed");
  assert.equal(failure.payload.code, "opencode_capability_unavailable");
  assert.equal(failure.payload.phase, "booting");
});

test("OpenCode bridge classifies provider responses without exposing their body", async () => {
  const paths = await fixture();
  const result = await runBridge({ ...baseEnvironment(paths), FOMO_TEST_MODEL_FAILURE: "1" });
  assert.equal(result.code, 1, result.stderr);
  assert.doesNotMatch(result.stdout, /model-private-value|provider leaked/);
  assert.doesNotMatch(result.stderr, /model-private-value|provider leaked/);

  const failure = envelopes(result.stdout).at(-1);
  assert.deepEqual(failure, {
    schemaVersion: 1,
    requestId: "request-1",
    correlationId: "run-1",
    seq: failure.seq,
    type: "failed",
    payload: {
      code: "opencode_model_failed",
      message: "OpenCode model request failed.",
      phase: "running",
    },
  });
});

test("OpenCode bridge classifies server failures without exposing exception text", async () => {
  const paths = await fixture();
  const result = await runBridge({ ...baseEnvironment(paths), FOMO_TEST_RUNTIME_FAILURE: "1" });
  assert.equal(result.code, 1, result.stderr);
  assert.doesNotMatch(result.stdout, /runtime-private-value|server leaked/);
  assert.doesNotMatch(result.stderr, /runtime-private-value|server leaked/);

  const failure = envelopes(result.stdout).at(-1);
  assert.deepEqual(failure, {
    schemaVersion: 1,
    requestId: "request-1",
    correlationId: "run-1",
    seq: 1,
    type: "failed",
    payload: {
      code: "opencode_runtime_failed",
      message: "OpenCode runtime could not complete the request.",
      phase: "booting",
    },
  });
});
