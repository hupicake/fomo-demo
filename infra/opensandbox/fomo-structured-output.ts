/**
 * Trusted planning-only structured-output extension for fomo-pi-ds.
 *
 * The control plane supplies one validated JSON Schema through the bridge's
 * bounded environment contract. Pi exposes that schema as a single virtual
 * form-like tool; its arguments are the machine output consumed by FOMO.
 */

import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type, type TSchema } from "typebox";

const SCHEMA_ENV = "FOMO_PI_STRUCTURED_OUTPUT_SCHEMA_B64";
const MAX_SCHEMA_BYTES = 64 * 1024;
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

function loadSchema(): TSchema {
	const encoded = process.env[SCHEMA_ENV];
	if (!encoded || !BASE64.test(encoded) || encoded.length % 4 !== 0) {
		throw new Error(`${SCHEMA_ENV} must be canonical base64`);
	}

	const bytes = Buffer.from(encoded, "base64");
	if (bytes.length === 0 || bytes.length > MAX_SCHEMA_BYTES || bytes.toString("base64") !== encoded) {
		throw new Error(`${SCHEMA_ENV} must decode to at most ${MAX_SCHEMA_BYTES} bytes`);
	}

	let schemaText: string;
	try {
		schemaText = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
	} catch {
		throw new Error(`${SCHEMA_ENV} must contain UTF-8 JSON`);
	}

	let schema: unknown;
	try {
		schema = JSON.parse(schemaText);
	} catch {
		throw new Error(`${SCHEMA_ENV} must contain valid JSON`);
	}
	if (
		!schema ||
		typeof schema !== "object" ||
		Array.isArray(schema) ||
		(schema as Record<string, unknown>).type !== "object"
	) {
		throw new Error(`${SCHEMA_ENV} must contain a root object JSON Schema`);
	}
	return schema as TSchema;
}

const parameters = Type.Unsafe<Record<string, unknown>>(loadSchema());

const submitStructuredOutput = defineTool({
	name: "submit_structured_output",
	label: "Submit Structured Output",
	description:
		"Submit the final planning contract by filling every field required by the provided schema.",
	promptSnippet: "Submit the final planning contract through its schema-backed form",
	promptGuidelines: [
		"Complete submit_structured_output successfully exactly once. If the form reports a schema validation error, correct it and retry, with at most 3 total attempts.",
		"Fill every required submit_structured_output field directly; do not return the contract as prose, Markdown, or a JSON code block.",
		"Stop immediately after submit_structured_output succeeds; do not emit another assistant response in the same turn.",
	],
	parameters,

	async execute(_toolCallId, params) {
		return {
			content: [{ type: "text" as const, text: "Structured planning output accepted." }],
			details: params,
			terminate: true,
		};
	},
});

export default function registerStructuredOutput(pi: ExtensionAPI) {
	pi.registerTool(submitStructuredOutput);
}
