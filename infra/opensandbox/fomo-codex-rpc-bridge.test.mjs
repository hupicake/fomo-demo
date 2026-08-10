import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { chmod, mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const BRIDGE = new URL("./fomo-codex-rpc-bridge.mjs", import.meta.url);
const MODEL_CATALOG = new URL("./fomo-codex-models.json", import.meta.url);
const THREAD_ID = "019fe958-a12e-7632-9817-efca95bf47e4";

const FAKE_CODEX = String.raw`#!/usr/bin/env node
import { appendFileSync, readFileSync } from "node:fs";

if (process.argv.includes("--version")) {
  process.stdout.write("codex-cli 0.147.0\n");
  process.exit(0);
}
let prompt = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { prompt += chunk; });
process.stdin.on("end", () => {
  const args = process.argv.slice(2);
  const schemaIndex = args.indexOf("--output-schema");
  const providerBase = args.find((value) => value.startsWith("model_providers.fomo_litellm.base_url="));
  appendFileSync(process.env.FAKE_CODEX_LOG, JSON.stringify({
    args,
    prompt,
    hasKey: process.env.CODEX_API_KEY === "sk-run-secret",
    openAiBaseUrl: process.env.OPENAI_BASE_URL,
    providerBase,
    codexHome: process.env.CODEX_HOME,
    fomoVariables: Object.keys(process.env).filter((name) => name.startsWith("FOMO_PI_")),
    schema: schemaIndex >= 0 ? readFileSync(args[schemaIndex + 1], "utf8").trim() : null,
  }) + "\n");
  const send = (value) => process.stdout.write(JSON.stringify(value) + "\n");
  const mode = process.env.FAKE_CODEX_MODE || "success";
  send({ type: "thread.started", thread_id: "${THREAD_ID}" });
  if (mode === "success") {
    send({ type: "item.completed", item: { id: "warning-1", type: "error", message: "recoverable private warning" } });
  }
  send({ type: "turn.started" });
  if (mode === "model-failed") {
    send({ type: "error", message: "provider leaked sk-run-secret" });
    send({ type: "turn.failed", error: { message: "private upstream failure" } });
    process.exitCode = 1;
    return;
  }
  if (mode === "command-eof-recovery" && !args.includes("resume")) {
    send({ type: "item.started", item: { id: "cmd-reused", type: "command_execution", command: "false" } });
    send({ type: "item.completed", item: { id: "cmd-reused", type: "command_execution", status: "failed", exit_code: 1, aggregated_output: "expected failure" } });
    return;
  }
  const text = mode === "structured" ? '{"answer":{"label":"ok","priority":"must"}}'
    : mode === "structured-invalid" ? "{" : "done";
  if (mode === "success") {
    send({ type: "item.completed", item: { id: "reason-1", type: "reasoning", text: "private reasoning" } });
    send({ type: "item.started", item: { id: "cmd-1", type: "command_execution", command: "echo sk-run-secret" } });
    send({ type: "item.updated", item: { id: "cmd-1", type: "command_execution", aggregated_output: "private command output" } });
    send({ type: "item.completed", item: { id: "cmd-1", type: "command_execution", status: "failed", exit_code: 1, aggregated_output: "private command output" } });
    send({ type: "error", message: "transient private provider error" });
  }
  if (mode === "command-eof-recovery") {
    send({ type: "item.started", item: { id: "cmd-reused", type: "command_execution", command: "true" } });
    send({ type: "item.completed", item: { id: "cmd-reused", type: "command_execution", status: "completed", exit_code: 0, aggregated_output: "fixed" } });
  }
  send({ type: "item.completed", item: { id: "message-1", type: "agent_message", text } });
  if (mode !== "protocol-eof") {
    const usage = args.includes("resume")
      ? { input_tokens: 160, cached_input_tokens: 30, cache_write_input_tokens: 7, output_tokens: 16 }
      : { input_tokens: 100, cached_input_tokens: 20, cache_write_input_tokens: 5, output_tokens: 10 };
    send({ type: "turn.completed", usage: { ...usage, reasoning_output_tokens: 5 } });
  }
});
`;

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "fomo-codex-bridge-"));
  const workspace = join(root, "workspace");
  const state = join(root, "state");
  const bin = join(root, "codex");
  const log = join(root, "codex.jsonl");
  await mkdir(workspace);
  await writeFile(bin, FAKE_CODEX);
  await chmod(bin, 0o755);
  return { root, workspace, state, bin, log };
}

function environment(paths, overrides = {}) {
  return {
    ...process.env,
    FAKE_CODEX_LOG: paths.log,
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
    FOMO_PI_MODEL_REF: "fomo-litellm/fomo-pi-gpt-5.6",
    FOMO_PI_CONTEXT_WINDOW: "250000",
    FOMO_PI_GRACE_SECONDS: "1",
    ...overrides,
  };
}

async function runBridge(env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [BRIDGE.pathname], { env });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`bridge test timed out\n${stdout}\n${stderr}`));
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

function records(stdout) {
  return stdout.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

async function invocations(path) {
  return (await readFile(path, "utf8")).trim().split("\n").map((line) => JSON.parse(line));
}

test("Codex bridge preserves the public lifecycle while keeping model IO private", async () => {
  const paths = await fixture();
  const result = await runBridge(environment(paths));
  assert.equal(result.code, 0, result.stderr);
  assert.doesNotMatch(result.stdout + result.stderr, /private prompt|sk-run-secret|private command|private reasoning/);

  const events = records(result.stdout);
  assert.equal(events[0].type, "started");
  assert.equal(events.at(-1).type, "completed");
  assert.deepEqual(events.at(-1).payload.stats.tokens, {
    input: 75, output: 10, cacheRead: 20, cacheWrite: 5, total: 110,
  });
  assert.equal("contextUsage" in events[0].payload.initialStats, false);
  assert.equal("contextUsage" in events.at(-1).payload.stats, false);
  assert.ok(events.some((event) => event.payload.kind === "tool_output" &&
    event.payload.text === "Codex tool execution is in progress."));
  assert.ok(events.some((event) => event.payload.kind === "tool_end" && event.payload.isError));

  const [call] = await invocations(paths.log);
  assert.equal(call.prompt, "private prompt");
  assert.equal(call.hasKey, true);
  assert.equal(call.openAiBaseUrl, undefined);
  assert.equal(call.providerBase, 'model_providers.fomo_litellm.base_url="http://litellm:4000/v1"');
  assert.deepEqual(call.fomoVariables, []);
  assert.deepEqual(call.args.slice(0, 10), [
    "--model", "fomo-pi-gpt-5.6", "--sandbox", "danger-full-access",
    "--ask-for-approval", "never", "--cd", paths.workspace, "--strict-config", "-c",
  ]);
  assert.ok(call.args.includes("exec"));
  assert.ok(!call.args.includes("--dangerously-bypass-approvals-and-sandbox"));
  assert.ok(call.args.includes('shell_environment_policy.set.PNPM_HOME="/opt/fomo/pnpm"'));
  assert.ok(call.args.includes('shell_environment_policy.set.PLAYWRIGHT_BROWSERS_PATH="/ms-playwright"'));
  assert.ok(call.args.includes("allow_login_shell=false"));
  assert.ok(call.args.includes("model_context_window=250000"));
  assert.ok(call.args.includes('model_catalog_json="/opt/fomo/bin/fomo-codex-models.json"'));
  assert.ok(call.args.includes('model_provider="fomo_litellm"'));
  assert.ok(call.args.includes('model_providers.fomo_litellm.env_key="CODEX_API_KEY"'));
  assert.ok(call.args.includes('model_providers.fomo_litellm.wire_api="responses"'));
  assert.ok(call.args.includes("model_providers.fomo_litellm.supports_websockets=false"));
  assert.doesNotMatch(JSON.stringify(call.args), /private prompt|sk-run-secret/);
});

test("Codex model catalog contains only the trusted FOMO GPT aliases", async () => {
  const catalog = JSON.parse(await readFile(MODEL_CATALOG, "utf8"));
  assert.deepEqual(catalog.models.map((model) => model.slug), [
    "fomo-pi-gpt-5.6",
    "fomo-pi-gpt-5.5",
  ]);
  for (const model of catalog.models) {
    assert.equal(model.context_window, 250000);
    assert.equal(model.max_context_window, 250000);
    assert.equal(model.supports_parallel_tool_calls, true);
    assert.equal(model.supports_reasoning_summaries, true);
    assert.deepEqual(model.supported_reasoning_levels.map(({ effort }) => effort), [
      "low", "medium", "high", "xhigh",
    ]);
    assert.match(model.base_instructions, /isolated generation sandbox/);
  }
});

test("Codex bridge resumes only the captured UUID and fail-closes a missing session", async () => {
  const paths = await fixture();
  assert.equal((await runBridge(environment(paths))).code, 0);
  const resumed = await runBridge(environment(paths, {
    FOMO_PI_PROMPT_B64: Buffer.from("continue safely").toString("base64"),
    FOMO_PI_REQUEST_ID: "request-2",
    FOMO_PI_REQUIRE_RESUME: "1",
  }));
  assert.equal(resumed.code, 0, resumed.stderr);
  const resumedEvents = records(resumed.stdout);
  assert.equal(resumedEvents[0].payload.resumed, true);
  assert.deepEqual(resumedEvents[0].payload.initialStats.tokens, {
    input: 75, output: 10, cacheRead: 20, cacheWrite: 5, total: 110,
  });
  assert.deepEqual(resumedEvents.at(-1).payload.stats.tokens, {
    input: 123, output: 16, cacheRead: 30, cacheWrite: 7, total: 176,
  });
  const calls = await invocations(paths.log);
  const resumeArgs = calls[1].args;
  assert.deepEqual(resumeArgs.slice(resumeArgs.indexOf("exec"), resumeArgs.indexOf("exec") + 2), ["exec", "resume"]);
  assert.ok(resumeArgs.includes(THREAD_ID));
  assert.ok(!resumeArgs.includes("--last"));
  assert.equal(calls[1].prompt, "continue safely");

  const missing = await fixture();
  const failed = await runBridge(environment(missing, { FOMO_PI_REQUIRE_RESUME: "1" }));
  assert.equal(failed.code, 1);
  assert.equal(records(failed.stdout).at(-1).payload.code, "session_resume_unavailable");
});

test("Codex bridge gives an incomplete failed command one bounded same-thread recovery", async () => {
  const paths = await fixture();
  const result = await runBridge(environment(paths, {
    FAKE_CODEX_MODE: "command-eof-recovery",
  }));
  assert.equal(result.code, 0, result.stderr);

  const events = records(result.stdout);
  assert.equal(events.filter((event) => event.type === "started").length, 1);
  assert.equal(events.filter((event) => event.type === "completed").length, 1);
  assert.deepEqual(
    events.filter((event) => event.payload.kind === "tool_end")
      .map((event) => event.payload.isError),
    [true, false],
  );
  assert.deepEqual(
    events.filter((event) => event.payload.kind === "tool_start")
      .map((event) => event.payload.toolCallId),
    ["cmd-reused", "recovery-1-cmd-reused"],
  );

  const calls = await invocations(paths.log);
  assert.equal(calls.length, 2);
  assert.ok(calls[1].args.includes("resume"));
  assert.ok(calls[1].args.includes(THREAD_ID));
  assert.ok(!calls[1].args.includes("--last"));
  assert.match(calls[1].prompt, /Inspect the failed command result/);
});

test("Codex bridge classifies structured, model, and protocol terminal failures", async () => {
  const unsupportedKeywords = [
    "default", "minLength", "maxLength", "pattern", "format",
    "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "multipleOf", "minProperties", "maxProperties",
    "patternProperties", "unevaluatedProperties", "propertyNames", "contains",
    "minContains", "maxContains", "dependentRequired", "dependentSchemas",
  ];
  const schema = {
    type: "object",
    title: "Structured answer",
    description: "Schema annotations stay available to the model.",
    $defs: {
      MustPriority: { type: "string", const: "must", enum: ["must", "legacy"] },
      ShouldPriority: { type: "string", const: "should" },
    },
    properties: {
      answer: {
        type: "object",
        properties: {
          label: { type: "string" },
          priority: {
            anyOf: [{ type: "null" }],
            oneOf: [
              { $ref: "#/$defs/MustPriority" },
              { $ref: "#/$defs/ShouldPriority" },
            ],
            discriminator: {
              propertyName: "kind",
              mapping: {
                must: "#/$defs/MustPriority",
                should: "#/$defs/ShouldPriority",
              },
            },
            ...Object.fromEntries(unsupportedKeywords.map((key) => [key, true])),
          },
        },
        required: ["label"],
      },
    },
    required: ["answer"],
  };
  const structuredPaths = await fixture();
  const structured = await runBridge(environment(structuredPaths, {
    FAKE_CODEX_MODE: "structured",
    FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64: Buffer.from(JSON.stringify(schema)).toString("base64"),
  }));
  assert.equal(structured.code, 0, structured.stderr);
  const structuredEvents = records(structured.stdout);
  const submit = structuredEvents.find((event) => event.payload.toolName === "submit_structured_output");
  assert.deepEqual(submit.payload.args, { answer: { label: "ok", priority: "must" } });
  const normalized = JSON.parse((await invocations(structuredPaths.log))[0].schema);
  assert.equal(normalized.title, schema.title);
  assert.equal(normalized.description, schema.description);
  assert.deepEqual(normalized.required, ["answer"]);
  assert.equal(normalized.additionalProperties, false);
  assert.deepEqual(normalized.properties.answer.required, ["label", "priority"]);
  assert.equal(normalized.properties.answer.additionalProperties, false);
  assert.equal(normalized.$defs.MustPriority.const, undefined);
  assert.deepEqual(normalized.$defs.MustPriority.enum, ["must"]);
  assert.equal(normalized.$defs.ShouldPriority.const, undefined);
  assert.deepEqual(normalized.$defs.ShouldPriority.enum, ["should"]);

  const priority = normalized.properties.answer.properties.priority;
  assert.equal(priority.oneOf, undefined);
  assert.equal(priority.discriminator, undefined);
  assert.deepEqual(priority.anyOf, [
    { type: "null" },
    { $ref: "#/$defs/MustPriority" },
    { $ref: "#/$defs/ShouldPriority" },
  ]);
  const unsupported = new Set(unsupportedKeywords);
  const assertStrictSubset = (value) => {
    if (Array.isArray(value)) return value.forEach(assertStrictSubset);
    if (!value || typeof value !== "object") return;
    for (const [key, child] of Object.entries(value)) {
      assert.equal(unsupported.has(key), false, `unsupported schema keyword ${key}`);
      assertStrictSubset(child);
    }
  };
  assertStrictSubset(normalized);

  for (const [mode, expected] of [
    ["model-failed", "codex_model_failed"],
    ["protocol-eof", "codex_protocol_failed"],
    ["structured-invalid", "codex_structured_output_invalid"],
  ]) {
    const paths = await fixture();
    const extra = mode === "structured-invalid" ? {
      FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64: Buffer.from(JSON.stringify(schema)).toString("base64"),
    } : {};
    const result = await runBridge(environment(paths, { FAKE_CODEX_MODE: mode, ...extra }));
    assert.equal(result.code, 1, `${mode}: ${result.stderr}`);
    assert.equal(records(result.stdout).at(-1).payload.code, expected);
    assert.doesNotMatch(result.stdout + result.stderr, /private upstream|provider leaked|sk-run-secret/);
  }
});
