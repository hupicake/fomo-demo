import assert from "node:assert/strict";
import {
	mkdirSync,
	mkdtempSync,
	readFileSync,
	realpathSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
	executeReadOnlySubtasks,
	resolveWorkspacePath,
	toAgentToolUsage,
} from "./fomo-delegate-subtasks-core.mjs";

const FAKE_PI = String.raw`#!/usr/bin/env node
import { appendFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const option = (name) => process.argv[process.argv.indexOf(name) + 1];
appendFileSync(process.env.FAKE_LOG, JSON.stringify({
  argv: process.argv.slice(2),
  key: process.env.FOMO_PI_VIRTUAL_KEY,
  child: process.env.FOMO_PI_DELEGATION_CHILD,
  hasPrompt: Boolean(process.env.FOMO_PI_PROMPT_B64),
  hasSession: Boolean(process.env.FOMO_PI_SESSION_ID),
}) + "\n");
writeFileSync(join(process.env.FAKE_START_DIR, String(process.pid)), "");
const deadline = Date.now() + 5000;
while (readdirSync(process.env.FAKE_START_DIR).length < Number(process.env.FAKE_EXPECTED_CHILDREN)) {
  if (Date.now() > deadline) process.exit(2);
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
}
if (process.env.FAKE_MODE === "slow") {
  setInterval(() => {}, 1000);
} else {
  const usage = (input, output) => ({
    input, output, cacheRead: 0, cacheWrite: 0, totalTokens: input + output,
    cost: { input: input / 1000, output: output / 1000, cacheRead: 0, cacheWrite: 0, total: (input + output) / 1000 },
  });
  const send = (event) => process.stdout.write(JSON.stringify(event) + "\n");
  send({ type: "agent_start" });
  if (process.env.FAKE_MODE !== "empty") {
    send({
      type: "message_end",
      message: { role: "assistant", content: [], stopReason: "toolUse", usage: usage(1, 2) },
    });
    send({ type: "tool_execution_end", toolCallId: "read-1", toolName: "read", isError: false, result: {} });
    send({ type: "compaction_end", result: { usage: usage(3, 4) }, aborted: false, willRetry: false });
    send({
      type: "message_end",
      message: {
        role: "assistant",
        content: [{ type: "text", text: "finding " + process.env.FOMO_PI_VIRTUAL_KEY }],
        stopReason: "stop",
        usage: usage(1, 2),
      },
    });
  }
  send({ type: "agent_settled" });
}
`;

function fixture() {
	const root = mkdtempSync(join(tmpdir(), "fomo-delegate-"));
	const workspace = join(root, "workspace");
	const starts = join(root, "starts");
	const fakePi = join(root, "fake-pi.mjs");
	const log = join(root, "calls.jsonl");
	mkdirSync(workspace);
	mkdirSync(starts);
	writeFileSync(fakePi, FAKE_PI, { mode: 0o700 });
	writeFileSync(log, "");
	return { root, workspace, starts, fakePi, log };
}

test("three children run concurrently with one frozen read-only contract and aggregate usage", async () => {
	const item = fixture();
	const progress = [];
	try {
		const result = await executeReadOnlySubtasks({
			tasks: [
				{ id: "architecture", task: "Inspect the architecture." },
				{ id: "state", task: "Inspect state ownership." },
				{ id: "tests", task: "Inspect existing tests." },
			],
			cwd: item.workspace,
			piBin: item.fakePi,
			modelRef: "fomo-litellm/fomo-pi-flash",
			thinkingLevel: "high",
			onProgress: (value) => progress.push(value),
			environment: {
				PATH: process.env.PATH,
				FOMO_PI_VIRTUAL_KEY: "test-run-secret",
				FOMO_PI_PROMPT_B64: "private-parent-prompt",
				FOMO_PI_SESSION_ID: "private-parent-session",
				FOMO_PI_WORKSPACE: item.workspace,
				FAKE_LOG: item.log,
				FAKE_START_DIR: item.starts,
				FAKE_EXPECTED_CHILDREN: "3",
			},
		});

		assert.deepEqual(result.results.map(({ id, status }) => ({ id, status })), [
			{ id: "architecture", status: "succeeded" },
			{ id: "state", status: "succeeded" },
			{ id: "tests", status: "succeeded" },
		]);
		assert.ok(result.results.every((child) => child.summary === "finding [redacted]"));
		assert.deepEqual(result.usage, {
			input: 15,
			output: 24,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 39,
			cost: { input: 0.015, output: 0.024, cacheRead: 0, cacheWrite: 0, total: 0.03900000000000001 },
			toolCalls: 3,
			turns: 6,
		});
		assert.deepEqual(toAgentToolUsage(result.usage), {
			input: 15,
			output: 24,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 39,
			cost: { input: 0.015, output: 0.024, cacheRead: 0, cacheWrite: 0, total: 0.03900000000000001 },
		});
		assert.equal(progress.at(0).completed, 0);
		assert.equal(progress.at(-1).completed, 3);

		const calls = readFileSync(item.log, "utf8").trim().split("\n").map(JSON.parse);
		assert.equal(calls.length, 3);
		for (const call of calls) {
			assert.equal(call.key, "test-run-secret");
			assert.equal(call.child, "1");
			assert.equal(call.hasPrompt, false);
			assert.equal(call.hasSession, false);
			assert.equal(call.argv[call.argv.indexOf("--model") + 1], "fomo-litellm/fomo-pi-flash");
			assert.equal(call.argv[call.argv.indexOf("--thinking") + 1], "high");
			assert.equal(call.argv[call.argv.indexOf("--tools") + 1], "read,grep,find,ls");
			for (const flag of [
				"--no-session", "--no-context-files", "--no-extensions", "--no-skills",
				"--no-prompt-templates", "--no-themes", "--no-approve", "--offline",
			]) assert.ok(call.argv.includes(flag), `missing ${flag}`);
			assert.match(call.argv[call.argv.indexOf("--extension") + 1], /fomo-delegate-readonly-guard\.ts$/);
		}
	} finally {
		rmSync(item.root, { recursive: true, force: true });
	}
});

test("empty lifecycle is not accepted as a successful child and cancellation stops a child", async () => {
	const item = fixture();
	try {
		const base = {
			tasks: [{ id: "empty", task: "Return nothing." }],
			cwd: item.workspace,
			piBin: item.fakePi,
			modelRef: "fomo-litellm/fomo-pi-flash",
			thinkingLevel: "high",
			environment: {
				PATH: process.env.PATH,
				FOMO_PI_VIRTUAL_KEY: "test-run-secret",
				FOMO_PI_WORKSPACE: item.workspace,
				FAKE_LOG: item.log,
				FAKE_START_DIR: item.starts,
				FAKE_EXPECTED_CHILDREN: "1",
				FAKE_MODE: "empty",
			},
		};
		const empty = await executeReadOnlySubtasks(base);
		assert.equal(empty.results[0].status, "failed");

		rmSync(item.starts, { recursive: true, force: true });
		mkdirSync(item.starts);
		const controller = new AbortController();
		const cancelledPromise = executeReadOnlySubtasks({
			...base,
			tasks: [{ id: "cancel", task: "Wait for cancellation." }],
			signal: controller.signal,
			environment: { ...base.environment, FAKE_MODE: "slow" },
		});
		setTimeout(() => controller.abort(), 100);
		const cancelled = await cancelledPromise;
		assert.equal(cancelled.results[0].status, "cancelled");
	} finally {
		rmSync(item.root, { recursive: true, force: true });
	}
});

test("workspace path resolution rejects absolute and symlink escapes", () => {
	const item = fixture();
	try {
		const inside = join(item.workspace, "inside.txt");
		const outside = join(item.root, "outside.txt");
		const escape = join(item.workspace, "escape.txt");
		writeFileSync(inside, "inside");
		writeFileSync(outside, "outside");
		symlinkSync(outside, escape);

		assert.equal(resolveWorkspacePath(item.workspace, "inside.txt"), realpathSync(inside));
		assert.throws(() => resolveWorkspacePath(item.workspace, outside), /escapes/);
		assert.throws(() => resolveWorkspacePath(item.workspace, escape), /escapes/);
		assert.throws(() => resolveWorkspacePath(item.workspace, "/proc/self/environ"));
	} finally {
		rmSync(item.root, { recursive: true, force: true });
	}
});
