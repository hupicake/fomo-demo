#!/usr/bin/env node

// Silent, bounded canary for the sandbox -> LiteLLM path. Never print a key,
// provider response, or thrown exception: the host trusts only the exit code.

const REQUEST_TIMEOUT_MS = 90_000;
const MAX_RESPONSE_BYTES = 256 * 1024;
const MAX_TOKENS = 128;
const TOOL_NAME = "fomo_runtime_canary";
const EXPECTED_ARGUMENTS = Object.freeze({ ready: true });
const AUTO_TOOL_CHOICE_ALIASES = new Set([
  "fomo-pi-deepseek-flash",
  "fomo-pi-flash",
]);
const KNOWN_ALIASES = new Set([
  "fomo-pi-gpt-5.6",
  "fomo-pi-gpt-5.5",
  "fomo-pi-deepseek-flash",
  "fomo-pi-grok-4.5",
  "fomo-pi-kimi-k2.7-code",
  "fomo-pi-gemini-3.6-flash",
  "fomo-pi-gemini-3.1-pro",
  "fomo-pi-flash",
  "fomo-pi-build",
]);

function requiredEnvironment(name) {
  const value = process.env[name];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error("invalid runtime preflight environment");
  }
  return value;
}

function providerBaseUrl() {
  const raw = requiredEnvironment("FOMO_PREFLIGHT_PROVIDER_BASE_URL");
  const parsed = new URL(raw);
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    !parsed.pathname.replace(/\/+$/, "").endsWith("/v1")
  ) {
    throw new Error("invalid runtime preflight provider URL");
  }
  return raw.replace(/\/+$/, "");
}

function virtualKey() {
  const value = requiredEnvironment("FOMO_PREFLIGHT_VIRTUAL_KEY");
  if (value.length > 4096 || /[\u0000-\u001f\u007f]/u.test(value)) {
    throw new Error("invalid runtime preflight key");
  }
  return value;
}

function modelAliases() {
  const parsed = JSON.parse(requiredEnvironment("FOMO_PREFLIGHT_ALIASES_JSON"));
  if (
    !Array.isArray(parsed) ||
    parsed.length === 0 ||
    parsed.length > KNOWN_ALIASES.size ||
    new Set(parsed).size !== parsed.length ||
    parsed.some((alias) => typeof alias !== "string" || !KNOWN_ALIASES.has(alias))
  ) {
    throw new Error("invalid runtime preflight aliases");
  }
  return parsed;
}

function toolCallDelta(frame, calls) {
  if (!Array.isArray(frame?.choices)) {
    throw new Error("invalid runtime preflight stream contract");
  }
  for (const choice of frame.choices) {
    const deltas = choice?.delta?.tool_calls;
    if (deltas === undefined) continue;
    if (!Array.isArray(deltas)) {
      throw new Error("invalid runtime preflight tool delta");
    }
    for (const delta of deltas) {
      if (!Number.isInteger(delta?.index) || delta.index < 0) {
        throw new Error("invalid runtime preflight tool index");
      }
      const call = calls.get(delta.index) || { name: "", arguments: "" };
      if (delta.function?.name !== undefined) {
        if (typeof delta.function.name !== "string") {
          throw new Error("invalid runtime preflight tool name");
        }
        call.name += delta.function.name;
      }
      if (delta.function?.arguments !== undefined) {
        if (typeof delta.function.arguments !== "string") {
          throw new Error("invalid runtime preflight tool arguments");
        }
        call.arguments += delta.function.arguments;
      }
      calls.set(delta.index, call);
    }
  }
}

function parseEvent(block, state) {
  const data = block
    .split(/\r?\n/u)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return;
  if (data === "[DONE]") {
    state.sawDone = true;
    return;
  }
  if (state.sawDone) {
    throw new Error("runtime preflight stream continued after done");
  }
  state.frames += 1;
  toolCallDelta(JSON.parse(data), state.calls);
}

async function boundedToolStream(response) {
  if (!response.headers.get("content-type")?.toLowerCase().includes("text/event-stream")) {
    await response.body?.cancel();
    throw new Error("runtime preflight response was not an event stream");
  }
  if (!response.body) {
    throw new Error("missing runtime preflight response body");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const state = { calls: new Map(), frames: 0, sawDone: false };
  let buffer = "";
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_RESPONSE_BYTES) {
      await reader.cancel();
      throw new Error("runtime preflight response exceeded limit");
    }
    buffer += decoder.decode(value, { stream: true });
    while (true) {
      const separator = buffer.search(/\r?\n\r?\n/u);
      if (separator < 0) break;
      const match = buffer.slice(separator).match(/^\r?\n\r?\n/u);
      const width = match?.[0].length || 2;
      parseEvent(buffer.slice(0, separator), state);
      buffer = buffer.slice(separator + width);
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) parseEvent(buffer, state);
  if (state.frames === 0 || !state.sawDone) {
    throw new Error("incomplete runtime preflight event stream");
  }
  const call = [...state.calls.values()].find((candidate) => candidate.name === TOOL_NAME);
  if (!call) {
    throw new Error("runtime preflight model did not call the required tool");
  }
  const args = JSON.parse(call.arguments);
  if (
    args?.ready !== EXPECTED_ARGUMENTS.ready ||
    Object.keys(args).length !== Object.keys(EXPECTED_ARGUMENTS).length
  ) {
    throw new Error("invalid runtime preflight tool arguments");
  }
}

async function complete(baseUrl, key, alias) {
  const response = await fetch(`${baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: alias,
      messages: [{
        role: "user",
        content: `Call ${TOOL_NAME} exactly once with ready=true. Do not answer in text.`,
      }],
      tools: [{
        type: "function",
        function: {
          name: TOOL_NAME,
          description: "Prove that streamed function tools work through the complete runtime route.",
          parameters: {
            type: "object",
            properties: { ready: { type: "boolean", const: true } },
            required: ["ready"],
            additionalProperties: false,
          },
        },
      }],
      // DeepSeek thinking rejects a forced function choice. "auto" still has
      // to produce the exact streamed canary call or this probe fails closed.
      tool_choice: AUTO_TOOL_CHOICE_ALIASES.has(alias)
        ? "auto"
        : { type: "function", function: { name: TOOL_NAME } },
      max_tokens: MAX_TOKENS,
      stream: true,
    }),
    redirect: "error",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error("runtime preflight completion failed");
  }
  await boundedToolStream(response);
}

async function main() {
  const baseUrl = providerBaseUrl();
  const key = virtualKey();
  for (const alias of modelAliases()) {
    await complete(baseUrl, key, alias);
  }
}

try {
  await main();
} catch {
  process.exitCode = 1;
}
