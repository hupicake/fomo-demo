/**
 * Trusted terminating user-input request for fomo-pi-ds.
 *
 * The tool is intentionally schema-backed and non-interactive: Pi ends the
 * current RPC turn, while the bridge publishes the validated request for the
 * control plane to persist and present. No extension UI request is used.
 */

import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const MAX_QUESTION_CHARACTERS = 2000;
const MAX_CHOICE_CHARACTERS = 200;
const MAX_CHOICES = 8;
const MAX_REASON_CHARACTERS = 1000;

const parameters = Type.Object(
	{
		question: Type.String({ minLength: 1, maxLength: MAX_QUESTION_CHARACTERS }),
		choices: Type.Optional(Type.Array(
			Type.String({ minLength: 1, maxLength: MAX_CHOICE_CHARACTERS }),
			{ maxItems: MAX_CHOICES },
		)),
		allowFreeform: Type.Boolean(),
		reason: Type.Optional(Type.String({ minLength: 1, maxLength: MAX_REASON_CHARACTERS })),
	},
	{ additionalProperties: false },
);

const requestUserInput = defineTool({
	name: "request_user_input",
	label: "Request User Input",
	description:
		"Pause this run at a clean turn boundary and request one decision that is required to continue safely.",
	promptSnippet: "Request a required user decision through the request_user_input form",
	promptGuidelines: [
		"Use request_user_input only when a missing user decision materially blocks correct delivery; resolve low-risk details autonomously.",
		"Put the complete public question in the form. Never imply that ordinary prose, a question mark, or hidden reasoning will pause the run.",
		"Provide concise choices when the decision has bounded options, and enable free-form input only when it is genuinely useful.",
		"Stop immediately after request_user_input succeeds; do not call another tool or emit another assistant response in the same turn.",
	],
	parameters,

	async execute(_toolCallId, params) {
		const question = params.question.trim();
		const choices = (params.choices ?? []).map((choice) => choice.trim());
		const reason = params.reason?.trim();
		if (!question || choices.some((choice) => !choice)) {
			throw new Error("Question and choices must not be blank.");
		}
		if (new Set(choices).size !== choices.length) {
			throw new Error("Choices must be unique.");
		}
		if (!params.allowFreeform && choices.length === 0) {
			throw new Error("Provide at least one choice or allow free-form input.");
		}
		if (params.reason !== undefined && !reason) {
			throw new Error("Reason must not be blank when provided.");
		}
		return {
			content: [{ type: "text" as const, text: "User input request accepted; ending this turn." }],
			details: {
				question,
				choices,
				allowFreeform: params.allowFreeform,
				...(reason ? { reason } : {}),
			},
			terminate: true,
		};
	},
});

export default function registerUserInput(pi: ExtensionAPI) {
	pi.registerTool(requestUserInput);
}
