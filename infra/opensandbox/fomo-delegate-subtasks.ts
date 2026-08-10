/**
 * Root-owned, read-only Pi delegation for one FOMO build/repair turn.
 *
 * The foreground Pi remains the sole writer and integrator. Each delegated
 * question runs in a fresh no-session Pi process with only read/grep/find/ls.
 */

import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import {
	executeReadOnlySubtasks,
	MAX_DELEGATED_TASKS,
	toAgentToolUsage,
} from "./fomo-delegate-subtasks-core.mjs";

const TASK_ID = "^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$";
let delegatedTaskCount = 0;

const parameters = Type.Object(
	{
		tasks: Type.Array(
			Type.Object(
				{
					id: Type.String({ minLength: 1, maxLength: 40, pattern: TASK_ID }),
					task: Type.String({ minLength: 1, maxLength: 2_000 }),
				},
				{ additionalProperties: false },
			),
			{ minItems: 1, maxItems: MAX_DELEGATED_TASKS },
		),
	},
	{ additionalProperties: false },
);

function requiredEnvironment(name: string): string {
	const value = process.env[name];
	if (!value) throw new Error(`Trusted delegation environment is missing ${name}.`);
	return value;
}

const delegateSubtasks = defineTool({
	name: "delegate_subtasks",
	label: "Delegate Read-only Subtasks",
	description:
		"Run up to three genuinely independent read-only codebase investigations in parallel, then return bounded findings for the parent agent to integrate.",
	promptSnippet: "Delegate independent read-only investigations when parallel research will materially save time",
	promptGuidelines: [
		"Use delegate_subtasks only when two or more independent codebase questions can be investigated without changing files; skip it for small or tightly coupled work.",
		"Give each child one bounded, non-overlapping question. Children cannot write, edit, run bash, load project instructions/extensions/skills, request input, keep sessions, or delegate again.",
		"Remain the sole writer: review the returned evidence, make all implementation and integration edits yourself, then run the required FOMO advisory QA.",
	],
	parameters,

	async execute(_toolCallId, params, signal, onUpdate, ctx) {
		if (process.env.FOMO_PI_DELEGATION_CHILD === "1") {
			throw new Error("Nested delegation is disabled.");
		}
		if (delegatedTaskCount + params.tasks.length > MAX_DELEGATED_TASKS) {
			throw new Error(`At most ${MAX_DELEGATED_TASKS} subtasks may be delegated in one Pi turn.`);
		}
		delegatedTaskCount += params.tasks.length;

		const workspace = requiredEnvironment("FOMO_PI_WORKSPACE");
		if (ctx.cwd !== workspace) {
			throw new Error("Delegation must use the frozen FOMO workspace.");
		}
		const result = await executeReadOnlySubtasks({
			tasks: params.tasks,
			cwd: workspace,
			piBin: requiredEnvironment("FOMO_PI_BIN"),
			modelRef: requiredEnvironment("FOMO_PI_MODEL_REF"),
			thinkingLevel:
				process.env.FOMO_PI_EFFECTIVE_THINKING_LEVEL ||
				requiredEnvironment("FOMO_PI_THINKING_LEVEL"),
			signal,
			onProgress(progress) {
				onUpdate?.({
					content: [{
						type: "text" as const,
						text: `Read-only parallel research: ${progress.completed}/${progress.total} complete.`,
					}],
					details: {
						schemaVersion: 1,
						kind: "fomo.delegate_subtasks.progress",
						completed: progress.completed,
						total: progress.total,
					},
				});
			},
		});

		const succeeded = result.results.filter((item) => item.status === "succeeded").length;
		const summaries = result.results.map(
			(item) => `### ${item.id} — ${item.status}\n\n${item.summary}`,
		);
		return {
			content: [{
				type: "text" as const,
				text: `Read-only parallel research: ${succeeded}/${result.results.length} succeeded.\n\n${summaries.join("\n\n---\n\n")}`,
			}],
			details: {
				schemaVersion: 1,
				kind: "fomo.delegate_subtasks.result",
				results: result.results.map((item) => ({
					id: item.id,
					status: item.status,
					usage: item.usage,
				})),
			},
			// Pi persists tool usage in the parent session. The bridge verifies
			// this aggregate and adds child read-tool calls to final telemetry.
			usage: toAgentToolUsage(result.usage),
		};
	},
});

export default function registerDelegateSubtasks(pi: ExtensionAPI) {
	if (process.env.FOMO_PI_DELEGATION_CHILD !== "1") {
		pi.registerTool(delegateSubtasks);
	}
}
