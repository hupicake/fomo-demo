import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { chmod, mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const BRIDGE = new URL("./fomo-opencode-rpc-bridge.mjs", import.meta.url);

const FAKE_SDK = String.raw`
let messageQueryCount = 0;
const assistant = {
  id: "assistant-1",
  sessionID: "oc-session-1",
  role: "assistant",
  parentID: "user-1",
  modelID: "fomo-pi-build",
  providerID: "fomo-litellm",
  mode: "build",
  agent: "build",
  path: { cwd: "/workspace", root: "/workspace" },
  cost: 0.001,
  tokens: { input: 10, output: 5, reasoning: 0, cache: { read: 0, write: 0 } },
  finish: "stop",
};
const user = {
  id: "user-1",
  sessionID: "oc-session-1",
  role: "user",
  time: { created: 1 },
  agent: "build",
  model: { providerID: "fomo-litellm", modelID: "fomo-pi-build" },
};

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
    event: {
      async subscribe(_parameters, options) {
        return {
          stream: (async function* () {
            yield { type: "server.connected", properties: {} };
            if (!process.env.FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64) {
              yield {
                type: "session.next.step.started",
                properties: { sessionID: "oc-session-1", assistantMessageID: "assistant-1" },
              };
              yield {
                type: "session.next.text.started",
                properties: {
                  sessionID: "oc-session-1", assistantMessageID: "assistant-1", textID: "text-1",
                },
              };
              yield {
                type: "session.next.text.delta",
                properties: {
                  sessionID: "oc-session-1", assistantMessageID: "assistant-1", textID: "text-1", delta: "done",
                },
              };
              yield {
                type: "session.next.text.ended",
                properties: {
                  sessionID: "oc-session-1", assistantMessageID: "assistant-1", textID: "text-1", text: "done",
                },
              };
              yield {
                type: "session.next.step.ended",
                properties: {
                  sessionID: "oc-session-1", assistantMessageID: "assistant-1", finish: "stop",
                },
              };
            }
            await new Promise((resolve) => {
              if (options.signal.aborted) resolve();
              else options.signal.addEventListener("abort", resolve, { once: true });
            });
          })(),
        };
      },
    },
    session: {
      async create(parameters) {
        if (parameters.model.providerID !== "fomo-litellm") throw new Error("wrong provider");
        return { data: { id: "oc-session-1" } };
      },
      async get() { return { data: { id: "oc-session-1" } }; },
      async messages() {
        messageQueryCount += 1;
        if (messageQueryCount === 1) return { data: [] };
        const info = process.env.FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64
          ? { ...assistant, structured: { answer: "ok" } }
          : assistant;
        return { data: [{ info: user, parts: [] }, { info, parts: [{ type: "text", text: "done" }] }] };
      },
      async prompt(parameters) {
        if (parameters.model.providerID !== "fomo-litellm") throw new Error("wrong prompt provider");
        if (process.env.FOMO_TEST_MODEL_FAILURE === "1") {
          return {
            data: {
              info: {
                ...assistant,
                error: {
                  name: "APIError",
                  data: {
                    message: "provider leaked api_key=model-private-value",
                    isRetryable: false,
                  },
                },
              },
              parts: [],
            },
          };
        }
        if (process.env.FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64) {
          if (parameters.format?.type !== "json_schema" || parameters.format.retryCount !== 2) {
            throw new Error("missing structured format");
          }
          if (Object.values(parameters.tools).some(Boolean)) throw new Error("structured tools enabled");
          return { data: { info: { ...assistant, structured: { answer: "ok" } }, parts: [] } };
        }
        return { data: { info: assistant, parts: [{ type: "text", text: "done" }] } };
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
    FOMO_PI_MODEL_REF: "fomo-litellm/fomo-pi-build",
    FOMO_PI_CONTEXT_WINDOW: "200000",
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
  assert.equal(result.code, 0, result.stderr);
  assert.equal(result.signal, null);
  assert.doesNotMatch(result.stdout, /private prompt|sk-run-secret/);
  assert.doesNotMatch(result.stderr, /private prompt|sk-run-secret/);

  const records = envelopes(result.stdout);
  assert.deepEqual(records.map((record) => record.seq), records.map((_, index) => index + 1));
  assert.equal(records[0].type, "started");
  assert.equal(records[0].payload.model, "fomo-litellm/fomo-pi-build");
  assert.ok(records.some((record) =>
    record.type === "pi.event" && record.payload.kind === "message_delta" && record.payload.delta === "done"));
  assert.equal(records.at(-1).type, "completed", JSON.stringify(records.at(-1)));
  assert.equal(records.at(-2).payload.kind, "agent_settled");
  assert.equal(records.at(-1).payload.stats.tokens.total, 15);

  const mapping = JSON.parse(await readFile(join(paths.state, "session-map", "session-1.json"), "utf8"));
  assert.deepEqual(mapping, {
    schemaVersion: 1,
    fomoSessionId: "session-1",
    openCodeSessionId: "oc-session-1",
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
