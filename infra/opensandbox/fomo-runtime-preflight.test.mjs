import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import test from "node:test";

const SCRIPT = fileURLToPath(
  new URL("./fomo-runtime-preflight.mjs", import.meta.url),
);
const ALIASES = ["fomo-pi-deepseek-flash", "fomo-pi-gpt-5.6"];
const SECRET = "sk-runtime-preflight-secret";
const TOOL_NAME = "fomo_runtime_canary";

function runProbe(baseUrl, aliases = ALIASES) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SCRIPT], {
      env: {
        FOMO_PREFLIGHT_PROVIDER_BASE_URL: `${baseUrl}/v1`,
        FOMO_PREFLIGHT_VIRTUAL_KEY: SECRET,
        FOMO_PREFLIGHT_ALIASES_JSON: JSON.stringify(aliases),
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code, signal) => {
      resolve({
        code,
        signal,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      });
    });
  });
}

function sendToolStream(response, { includeTool = true, includeDone = true } = {}) {
  response.writeHead(200, { "content-type": "text/event-stream" });
  const deltas = includeTool
    ? [
        { index: 0, id: "call_canary", type: "function", function: { name: TOOL_NAME, arguments: "{\"ready\":" } },
        { index: 0, function: { arguments: "true}" } },
      ]
    : [];
  for (const delta of deltas) {
    response.write(`data: ${JSON.stringify({ choices: [{ index: 0, delta: { tool_calls: [delta] } }] })}\n\n`);
  }
  response.write(`data: ${JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: includeTool ? "tool_calls" : "stop" }] })}\n\n`);
  if (includeDone) response.write("data: [DONE]\n\n");
  response.end();
}

async function withServer(handler, callback) {
  const server = createServer(handler);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const address = server.address();
    assert.equal(typeof address, "object");
    return await callback(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
}

test("sandbox runtime probe proves streaming tool calls for every enabled alias", async () => {
  const seen = [];
  const result = await withServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    seen.push({
      authorization: request.headers.authorization,
      path: request.url,
      body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
    });
    sendToolStream(response);
  }, runProbe);

  assert.deepEqual(
    seen.map((request) => request.body.model),
    ALIASES,
  );
  assert.ok(seen.every((request) => request.path === "/v1/chat/completions"));
  assert.ok(
    seen.every((request) => request.authorization === `Bearer ${SECRET}`),
  );
  assert.ok(seen.every((request) => request.body.max_tokens === 128));
  assert.ok(seen.every((request) => request.body.stream === true));
  assert.ok(seen.every((request) => request.body.tools?.[0]?.function?.name === TOOL_NAME));
  assert.equal(seen[0].body.tool_choice, "auto");
  assert.equal(seen[1].body.tool_choice?.function?.name, TOOL_NAME);
  assert.equal(result.code, 0);
  assert.equal(result.signal, null);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "");
});

test("sandbox runtime probe rejects a non-streaming OK response", async () => {
  const result = await withServer((_request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ choices: [{ message: { content: "OK" } }] }));
  }, runProbe);

  assert.equal(result.code, 1);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "");
});

test("sandbox runtime probe rejects a stream without the required tool", async () => {
  const result = await withServer((_request, response) => {
    sendToolStream(response, { includeTool: false });
  }, runProbe);

  assert.equal(result.code, 1);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "");
});

test("sandbox runtime probe rejects aliases outside the fail-closed catalog", async () => {
  let requested = false;
  const result = await withServer((_request, response) => {
    requested = true;
    sendToolStream(response);
  }, (baseUrl) => runProbe(baseUrl, ["unscoped-provider-model"]));

  assert.equal(requested, false);
  assert.equal(result.code, 1);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "");
});

test("sandbox runtime probe never prints a provider failure body or key", async () => {
  const result = await withServer((_request, response) => {
    response.writeHead(503, { "content-type": "text/plain" });
    response.end(`${SECRET} private provider response`);
  }, runProbe);

  assert.equal(result.code, 1);
  assert.equal(result.signal, null);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "");
});
