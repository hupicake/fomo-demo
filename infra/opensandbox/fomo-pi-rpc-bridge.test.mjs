import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";

const DIRECTORY = dirname(fileURLToPath(import.meta.url));
const BRIDGE = join(DIRECTORY, "fomo-pi-rpc-bridge.mjs");
const EXTENSION = join(DIRECTORY, "fomo-structured-output.ts");
const USER_INPUT_EXTENSION = join(DIRECTORY, "fomo-request-user-input.ts");
const DELEGATE_EXTENSION = join(DIRECTORY, "fomo-delegate-subtasks.ts");

const FAKE_PI = String.raw`#!/usr/bin/env node
import { appendFileSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const option = (name) => process.argv[process.argv.indexOf(name) + 1];
const sessionId = option("--session-id");
const modelRef = option("--model");
const thinkingLevel = option("--thinking");
appendFileSync(process.env.FAKE_PI_ARGV_FILE, JSON.stringify(process.argv.slice(2)));
const settingsPath = join(process.env.PI_CODING_AGENT_DIR, "settings.json");
appendFileSync(process.env.FAKE_PI_SETTINGS_FILE, JSON.stringify({
  contents: JSON.parse(readFileSync(settingsPath, "utf8")),
  mode: statSync(settingsPath).mode & 0o777,
}));

const send = (value) => process.stdout.write(JSON.stringify(value) + "\n");
const state = () => ({
  model: { provider: "fomo-litellm", id: modelRef.split("/")[1] },
  thinkingLevel,
  sessionId,
  messageCount: Number(process.env.FAKE_PI_MESSAGE_COUNT || 0),
  pendingMessageCount: 0,
  isStreaming: false,
  isCompacting: false,
});
let delegationDone = false;
const stats = () => delegationDone ? ({
  sessionId,
  userMessages: 1,
  assistantMessages: 1,
  toolCalls: 1,
  toolResults: 1,
  totalMessages: 3,
  tokens: { input: 30, output: 10, cacheRead: 3, cacheWrite: 1, total: 44 },
  cost: 0.044,
}) : ({
  sessionId,
  userMessages: 0,
  assistantMessages: 0,
  toolCalls: 0,
  toolResults: 0,
  totalMessages: 0,
  tokens: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  cost: 0,
});

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  input += chunk;
  let newline = input.indexOf("\n");
  while (newline >= 0) {
    const command = JSON.parse(input.slice(0, newline));
    input = input.slice(newline + 1);
    const type = command.type;
    if (type === "set_model" || type === "set_thinking_level") {
      send({ type: "response", id: command.id, command: type, success: true });
    } else if (type === "get_state") {
      send({ type: "response", id: command.id, command: type, success: true, data: state() });
    } else if (type === "get_session_stats") {
      send({ type: "response", id: command.id, command: type, success: true, data: stats() });
    } else if (type === "prompt") {
      send({ type: "response", id: command.id, command: type, success: true });
      send({ type: "agent_start" });
      const mode = process.env.FAKE_PI_MODE;
      let settleImmediately = true;
      const output = process.env.FAKE_PI_OUTPUT_B64
        ? JSON.parse(Buffer.from(process.env.FAKE_PI_OUTPUT_B64, "base64").toString("utf8"))
        : { answer: "ok" };
      const submit = (id, args, isError) => {
        send({
          type: "tool_execution_start",
          toolCallId: id,
          toolName: "submit_structured_output",
          args,
        });
        send({
          type: "tool_execution_end",
          toolCallId: id,
          toolName: "submit_structured_output",
          isError,
        });
      };
      const requestInput = (id, args, isError) => {
        send({
          type: "tool_execution_start",
          toolCallId: id,
          toolName: "request_user_input",
          args,
        });
        send({
          type: "tool_execution_end",
          toolCallId: id,
          toolName: "request_user_input",
          isError,
        });
      };
      const delegate = (invalid = false) => {
        const tasks = [
          { id: "architecture", task: "inspect private repository detail test-virtual-key" },
          { id: "tests", task: "inspect independent tests" },
        ];
        const childUsage = [
          {
            input: 10, output: 4, cacheRead: 1, cacheWrite: 0, totalTokens: 15,
            cost: { input: 0.01, output: 0.004, cacheRead: 0.001, cacheWrite: 0, total: 0.015 },
            toolCalls: 2, turns: 1,
          },
          {
            input: 20, output: 6, cacheRead: 2, cacheWrite: 1, totalTokens: 29,
            cost: { input: 0.02, output: 0.006, cacheRead: 0.002, cacheWrite: 0.001, total: 0.029 },
            toolCalls: 3, turns: 1,
          },
        ];
        const aggregate = {
          input: 30, output: 10, cacheRead: 3, cacheWrite: 1,
          totalTokens: invalid ? 43 : 44,
          cost: { input: 0.03, output: 0.01, cacheRead: 0.003, cacheWrite: 0.001, total: 0.044 },
        };
        send({ type: "tool_execution_start", toolCallId: "delegate-1", toolName: "delegate_subtasks", args: { tasks } });
        send({
          type: "tool_execution_update",
          toolCallId: "delegate-1",
          toolName: "delegate_subtasks",
          args: { tasks },
          partialResult: { content: [{ type: "text", text: "Read-only parallel research: 1/2 complete." }] },
        });
        send({
          type: "tool_execution_end",
          toolCallId: "delegate-1",
          toolName: "delegate_subtasks",
          isError: false,
          result: {
            content: [{ type: "text", text: "private child findings test-virtual-key" }],
            details: {
              schemaVersion: 1,
              kind: "fomo.delegate_subtasks.result",
              results: tasks.map((task, index) => ({ id: task.id, status: "succeeded", usage: childUsage[index] })),
            },
            usage: aggregate,
          },
        });
        delegationDone = true;
      };
      if (mode === "delegate" || mode === "delegate-invalid") {
        delegate(mode === "delegate-invalid");
      } else if (mode === "structured") {
        submit("structured-1", output, false);
      } else if (mode === "structured-retry") {
        submit("structured-1", { answer: 42 }, true);
        submit("structured-2", output, false);
      } else if (mode === "structured-all-failed") {
        submit("structured-1", { answer: 1 }, true);
        submit("structured-2", { answer: 2 }, true);
        submit("structured-3", { answer: 3 }, true);
      } else if (mode === "structured-after-success") {
        submit("structured-1", output, false);
        submit("structured-2", output, false);
      } else if (mode === "structured-too-many") {
        submit("structured-1", { answer: 1 }, true);
        submit("structured-2", { answer: 2 }, true);
        submit("structured-3", { answer: 3 }, true);
        submit("structured-4", output, false);
      } else if (mode === "structured-native") {
        send({
          type: "tool_execution_start",
          toolCallId: "native-1",
          toolName: "read",
          args: { path: "package.json" },
        });
      } else if (mode === "structured-unmatched") {
        send({
          type: "tool_execution_end",
          toolCallId: "structured-missing",
          toolName: "submit_structured_output",
          isError: false,
        });
      } else if (mode === "input-request" || mode === "structured-input-request") {
        requestInput("input-1", output, false);
      } else if (mode === "input-request-invalid-success") {
        requestInput("input-1", output, false);
      } else if (mode === "input-request-after-success") {
        requestInput("input-1", output, false);
        send({
          type: "tool_execution_start",
          toolCallId: "native-after-input",
          toolName: "read",
          args: { path: "package.json" },
        });
      } else if (mode === "plain-question") {
        send({
          type: "message_update",
          assistantMessageEvent: {
            type: "text_delta",
            contentIndex: 0,
            delta: "Which option should I use?",
          },
        });
      } else if (mode === "streaming-thinking") {
        settleImmediately = false;
        let updates = 0;
        const timer = setInterval(() => {
          updates += 1;
          send({
            type: "message_update",
            assistantMessageEvent: { type: "thinking_delta", delta: "private reasoning" },
          });
          if (updates === 4) {
            clearInterval(timer);
            send({
              type: "message_update",
              assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "ok" },
            });
            send({ type: "agent_settled" });
          }
        }, 300);
      } else if (mode === "silent") {
        settleImmediately = false;
        setTimeout(() => send({ type: "agent_settled" }), 1_200);
      } else if (mode !== "missing-structured") {
        send({
          type: "message_update",
          assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "ok" },
        });
      }
      if (settleImmediately) send({ type: "agent_settled" });
    } else if (type === "abort") {
      process.exit(0);
    }
    newline = input.indexOf("\n");
  }
});
`;

function encode(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return Buffer.from(text).toString("base64");
}

function runBridge({
  schema,
  mode = "normal",
  output,
  userInput = false,
  requireResume = false,
  messageCount = 0,
  activitySilenceSeconds,
  thinkingLevel = "max",
} = {}) {
  const root = mkdtempSync(join(tmpdir(), "fomo-pi-bridge-"));
  const workspace = join(root, "workspace");
  const state = join(root, "state");
  const fakePi = join(root, "fake-pi.mjs");
  const argvFile = join(root, "argv.json");
  const settingsFile = join(root, "settings-snapshot.json");
  writeFileSync(fakePi, FAKE_PI, { mode: 0o700 });
  writeFileSync(argvFile, "");
  writeFileSync(settingsFile, "");
  mkdirSync(workspace);

  const environment = {
    PATH: process.env.PATH || "",
    FOMO_PI_PROMPT_B64: encode("plan the product"),
    FOMO_PI_SESSION_ID: "session-1",
    FOMO_PI_REQUEST_ID: "request-1",
    FOMO_PI_CORRELATION_ID: "run-1",
    FOMO_PI_PROVIDER_BASE_URL: "http://litellm:4000/v1",
    FOMO_PI_VIRTUAL_KEY: "test-virtual-key",
    FOMO_PI_WORKSPACE: workspace,
    FOMO_PI_STATE_DIR: state,
    FOMO_PI_BIN: fakePi,
    FOMO_PI_MODEL_REF: "fomo-litellm/fomo-pi-flash",
    FOMO_PI_THINKING_LEVEL: thinkingLevel,
    FOMO_PI_GRACE_SECONDS: "1",
    FAKE_PI_ARGV_FILE: argvFile,
    FAKE_PI_SETTINGS_FILE: settingsFile,
    FAKE_PI_MODE: mode,
    FAKE_PI_MESSAGE_COUNT: String(messageCount),
  };
  if (schema !== undefined) environment.FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64 = encode(schema);
  if (output !== undefined) environment.FAKE_PI_OUTPUT_B64 = encode(output);
  if (userInput) environment.FOMO_PI_USER_INPUT_ENABLED = "1";
  if (requireResume) environment.FOMO_PI_REQUIRE_RESUME = "1";
  if (activitySilenceSeconds !== undefined) {
    environment.FOMO_PI_ACTIVITY_SILENCE_SECONDS = String(activitySilenceSeconds);
  }

  const completed = spawnSync(process.execPath, [BRIDGE], {
    cwd: workspace,
    env: environment,
    encoding: "utf8",
    timeout: 10_000,
  });
  const records = completed.stdout.trim()
    ? completed.stdout.trim().split("\n").map((line) => JSON.parse(line))
    : [];
  const argv = readFileSync(argvFile, "utf8") ? JSON.parse(readFileSync(argvFile, "utf8")) : [];
  const settings = readFileSync(settingsFile, "utf8")
    ? JSON.parse(readFileSync(settingsFile, "utf8"))
    : null;
  const stateEntries = existsSync(state) ? readdirSync(state).sort() : [];
  const sessionsDir = join(state, "sessions");
  const sessionEntries = existsSync(sessionsDir) ? readdirSync(sessionsDir).sort() : [];
  rmSync(root, { recursive: true, force: true });
  return { completed, records, argv, settings, stateEntries, sessionEntries };
}

test("non-planning mode adds only the trusted read-only delegation tool", () => {
  const { completed, records, argv, settings, stateEntries, sessionEntries } = runBridge();
  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(
    argv[argv.indexOf("--tools") + 1],
    "read,write,edit,bash,grep,find,ls,delegate_subtasks",
  );
  assert.deepEqual(
    argv.flatMap((value, index) => value === "--extension" ? [argv[index + 1]] : []),
    [DELEGATE_EXTENSION],
  );
  assert.equal(argv.includes("--no-extensions"), true);
  assert.equal(records.find((record) => record.type === "started").payload.contextWindow, 200_000);
  assert.deepEqual(settings, {
    contents: {
      compaction: {
        enabled: true,
        reserveTokens: 32_768,
        keepRecentTokens: 20_000,
      },
    },
    mode: 0o600,
  });
  assert.equal(JSON.stringify(settings).includes("test-virtual-key"), false);
  assert.deepEqual(stateEntries, ["sessions"]);
  assert.deepEqual(sessionEntries, []);
  assert.equal(records.at(-1).type, "completed");
});

test("high-thinking stream activity prevents a false inactivity timeout", () => {
  const { completed, records } = runBridge({
    mode: "streaming-thinking",
    thinkingLevel: "high",
    activitySilenceSeconds: 1,
  });

  assert.equal(completed.status, 0, completed.stderr);
  assert.ok(records.some(
    (record) => record.type === "pi.event" && record.payload.kind === "inference_heartbeat",
  ));
  assert.equal(JSON.stringify(records).includes("private reasoning"), false);
  assert.equal(records.at(-1).type, "completed");
});

test("a silent but connected Pi stream stays alive until it settles", () => {
  const { completed, records } = runBridge({
    mode: "silent",
    thinkingLevel: "high",
    activitySilenceSeconds: 1,
  });

  assert.equal(completed.status, 0, completed.stderr);
  assert.ok(records.some(
    (record) => record.type === "pi.event" && record.payload.kind === "inference_heartbeat",
  ));
  assert.equal(records.at(-1).type, "completed");
});

test("delegation publishes bounded progress and preserves usage in parent stats", () => {
  const { completed, records } = runBridge({ mode: "delegate" });

  assert.equal(completed.status, 0, completed.stderr);
  const start = records.find(
    (record) => record.type === "pi.event" &&
      record.payload.kind === "tool_start" &&
      record.payload.toolName === "delegate_subtasks",
  );
  assert.deepEqual(start.payload.args, {
    tasks: [{ id: "architecture" }, { id: "tests" }],
  });
  const output = records.find(
    (record) => record.type === "pi.event" &&
      record.payload.kind === "tool_output" &&
      record.payload.toolName === "delegate_subtasks",
  );
  assert.equal(output.payload.text, "Read-only parallel research: 1/2 complete.");
  assert.equal(JSON.stringify(records).includes("private repository detail"), false);
  assert.equal(JSON.stringify(records).includes("private child findings"), false);
  assert.equal(JSON.stringify(records).includes("test-virtual-key"), false);

  const result = records.at(-1);
  assert.equal(result.type, "completed");
  assert.deepEqual(result.payload.stats.tokens, {
    input: 30, output: 10, cacheRead: 3, cacheWrite: 1, total: 44,
  });
  assert.equal(result.payload.stats.cost, 0.044);
  // Child activity is telemetry rather than fake persisted Pi session rows.
  assert.equal(result.payload.stats.toolCalls, 1);
  assert.deepEqual(result.payload.telemetry.delegation, {
    requestedTasks: 2,
    completedTasks: 2,
    childTurns: 2,
    childToolCalls: 5,
  });
});

test("delegation fails closed when child and aggregate usage disagree", () => {
  const { completed, records } = runBridge({ mode: "delegate-invalid" });

  assert.notEqual(completed.status, 0);
  assert.equal(records.at(-1).type, "failed");
  assert.equal(records.at(-1).payload.code, "invalid_delegation");
});

test("user-input mode exposes one trusted terminating form and publishes a safe request", () => {
  const extensionSource = readFileSync(USER_INPUT_EXTENSION, "utf8");
  assert.match(extensionSource, /additionalProperties: false/);
  assert.match(extensionSource, /terminate: true/);
  assert.doesNotMatch(extensionSource, /ctx\.ui/);
  const output = {
    question: "  Which data source should the dashboard use?  ",
    choices: ["  Production API  ", "Mock fixtures"],
    allowFreeform: false,
    reason: "  The two sources have incompatible fields.  ",
  };
  const { completed, records, argv } = runBridge({
    mode: "input-request",
    output,
    userInput: true,
  });

  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(
    argv[argv.indexOf("--tools") + 1],
    "read,write,edit,bash,grep,find,ls,delegate_subtasks,request_user_input",
  );
  const extensions = argv.flatMap((value, index) => value === "--extension" ? [argv[index + 1]] : []);
  assert.deepEqual(extensions, [DELEGATE_EXTENSION, USER_INPUT_EXTENSION]);
  const toolEndIndex = records.findIndex(
    (record) => record.type === "pi.event" &&
      record.payload.kind === "tool_end" &&
      record.payload.toolName === "request_user_input",
  );
  const requestIndex = records.findIndex(
    (record) => record.type === "pi.event" && record.payload.kind === "input_request",
  );
  assert.ok(toolEndIndex >= 0);
  assert.ok(requestIndex > toolEndIndex);
  const inputRequest = records[requestIndex].payload.inputRequest;
  assert.match(inputRequest.requestId, /^input-[0-9a-f-]{36}$/);
  assert.deepEqual(inputRequest, {
    requestId: inputRequest.requestId,
    question: "Which data source should the dashboard use?",
    choices: ["Production API", "Mock fixtures"],
    allowFreeform: false,
    reason: "The two sources have incompatible fields.",
  });
  assert.deepEqual(records.at(-1).payload.inputRequest, inputRequest);
});

test("ordinary question text never becomes a user-input request", () => {
  const { completed, records } = runBridge({ mode: "plain-question", userInput: true });

  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(
    records.some(
      (record) => record.type === "pi.event" && record.payload.kind === "input_request",
    ),
    false,
  );
  assert.equal(records.at(-1).payload.inputRequest, null);
});

test("user-input mode fails closed when a successful tool result has invalid arguments", () => {
  const { completed, records } = runBridge({
    mode: "input-request-invalid-success",
    userInput: true,
    output: {
      question: "Choose a source",
      choices: [],
      allowFreeform: true,
      unexpected: "must not escape",
    },
  });

  assert.notEqual(completed.status, 0);
  assert.equal(records.at(-1).type, "failed");
  assert.equal(records.at(-1).payload.code, "invalid_user_input_request");
  assert.equal(JSON.stringify(records).includes("must not escape"), false);
});

test("planning mode may request input instead of fabricating structured output", () => {
  const schema = { type: "object", properties: {}, additionalProperties: false };
  const output = {
    question: "Which audience is primary?",
    choices: ["Operators", "Managers"],
    allowFreeform: true,
  };
  const { completed, records, argv } = runBridge({
    schema,
    mode: "structured-input-request",
    output,
    userInput: true,
  });

  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(
    argv[argv.indexOf("--tools") + 1],
    "submit_structured_output,request_user_input",
  );
  const extensions = argv.flatMap((value, index) => value === "--extension" ? [argv[index + 1]] : []);
  assert.deepEqual(extensions, [EXTENSION, USER_INPUT_EXTENSION]);
  assert.equal(records.at(-1).payload.inputRequest.question, output.question);
});

test("required session continuation fails before sending a prompt when history is absent", () => {
  const { completed, records } = runBridge({ requireResume: true, messageCount: 0 });

  assert.notEqual(completed.status, 0);
  assert.equal(records.some((record) => record.type === "started"), false);
  assert.equal(records.at(-1).type, "failed");
  assert.equal(records.at(-1).payload.code, "session_resume_unavailable");
});

test("required session continuation proceeds when Pi reports prior messages", () => {
  const { completed, records } = runBridge({ requireResume: true, messageCount: 2 });

  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(records.find((record) => record.type === "started").payload.resumed, true);
});

test("structured mode exposes only the terminating schema tool and preserves its complete arguments", () => {
  const extensionSource = readFileSync(EXTENSION, "utf8");
  assert.doesNotMatch(extensionSource, /at most 3 total attempts/);
  assert.match(extensionSource, /Stop immediately after submit_structured_output succeeds/);
  assert.doesNotMatch(extensionSource, /exactly once as the final action/);
  const schema = {
    type: "object",
    additionalProperties: false,
    required: ["title", "notes", "items"],
    properties: {
      title: { type: "string" },
      notes: { type: "string" },
      items: { type: "array", items: { type: "string" } },
    },
  };
  const output = {
    title: "GoalGraph",
    notes: "x".repeat(4096),
    items: Array.from({ length: 140 }, (_, index) => `item-${index}`),
  };
  const { completed, records, argv } = runBridge({ schema, mode: "structured", output });
  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(argv[argv.indexOf("--tools") + 1], "submit_structured_output");
  assert.equal(argv[argv.indexOf("--extension") + 1], EXTENSION);
  assert.equal(argv.includes("--no-extensions"), true);

  const toolStart = records.find(
    (record) => record.type === "pi.event" && record.payload.kind === "tool_start",
  );
  assert.equal(toolStart.payload.toolName, "submit_structured_output");
  assert.deepEqual(toolStart.payload.args, output);
  assert.equal(records.at(-1).type, "completed");
});

test("structured mode preserves a failed form result and accepts one corrected retry", () => {
  const schema = {
    type: "object",
    additionalProperties: false,
    required: ["answer"],
    properties: { answer: { type: "string" } },
  };
  const output = { answer: "valid" };
  const { completed, records } = runBridge({ schema, mode: "structured-retry", output });

  assert.equal(completed.status, 0, completed.stderr);
  const starts = records.filter(
    (record) => record.type === "pi.event" && record.payload.kind === "tool_start",
  );
  const ends = records.filter(
    (record) => record.type === "pi.event" && record.payload.kind === "tool_end",
  );
  assert.deepEqual(starts.map((record) => record.payload.args), [
    { answer: 42 },
    output,
  ]);
  assert.deepEqual(ends.map((record) => record.payload.isError), [true, false]);
  assert.equal(records.at(-1).type, "completed");
});

test("structured mode fails closed when the agent settles without a valid form", () => {
  const schema = { type: "object", properties: {}, additionalProperties: false };
  const { completed, records } = runBridge({ schema, mode: "structured-all-failed" });

  assert.notEqual(completed.status, 0);
  const ends = records.filter(
    (record) => record.type === "pi.event" && record.payload.kind === "tool_end",
  );
  assert.equal(ends.length, 3);
  assert.ok(ends.every((record) => record.payload.isError === true));
  assert.equal(records.at(-1).type, "failed");
  assert.equal(records.at(-1).payload.code, "missing_structured_output");
});

test("structured mode rejects any tool call after its successful submission", () => {
  const schema = { type: "object", properties: {}, additionalProperties: false };
  const { completed, records } = runBridge({
    schema,
    mode: "structured-after-success",
    output: {},
  });

  assert.notEqual(completed.status, 0);
  assert.equal(records.at(-1).type, "failed");
  assert.equal(records.at(-1).payload.code, "invalid_structured_output");
});

test("structured mode allows a fourth form attempt to self-correct", () => {
  const schema = { type: "object", properties: {}, additionalProperties: false };
  const { completed, records } = runBridge({ schema, mode: "structured-too-many" });

  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(records.at(-1).type, "completed");
});

for (const [mode, name] of [
  ["structured-native", "a native tool"],
  ["structured-unmatched", "an unmatched tool result"],
]) {
  test(`structured mode rejects ${name}`, () => {
    const schema = { type: "object", properties: {}, additionalProperties: false };
    const { completed, records } = runBridge({ schema, mode });

    assert.notEqual(completed.status, 0);
    assert.equal(records.at(-1).type, "failed");
    assert.equal(records.at(-1).payload.code, "invalid_tool_lifecycle");
  });
}

test("structured mode fails closed when the model does not submit the virtual tool", () => {
  const schema = { type: "object", properties: {}, additionalProperties: false };
  const { completed, records } = runBridge({ schema, mode: "missing-structured" });
  assert.notEqual(completed.status, 0);
  assert.equal(records.at(-1).type, "failed");
  assert.equal(records.at(-1).payload.code, "missing_structured_output");
});

test("structured mode rejects a non-object JSON Schema before Pi starts", () => {
  const { completed, records, argv } = runBridge({ schema: ["not", "an", "object"] });
  assert.notEqual(completed.status, 0);
  assert.deepEqual(argv, []);
  assert.equal(records.at(-1).type, "failed");
  assert.equal(records.at(-1).payload.code, "invalid_environment");
});
