/** Root-owned path guard for read-only delegated Pi processes. */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { resolveWorkspacePath } from "./fomo-delegate-subtasks-core.mjs";

const READ_ONLY_TOOLS = new Set(["read", "grep", "find", "ls"]);

export default function registerReadOnlyGuard(pi: ExtensionAPI) {
	pi.on("tool_call", async (event, ctx) => {
		const workspace = process.env.FOMO_PI_WORKSPACE;
		if (!workspace || ctx.cwd !== workspace || !READ_ONLY_TOOLS.has(event.toolName)) {
			return { block: true, reason: "Delegated tools are restricted to the frozen workspace." };
		}
		const input = event.input as Record<string, unknown>;
		const candidate = input.path === undefined ? "." : input.path;
		try {
			resolveWorkspacePath(workspace, candidate);
			return undefined;
		} catch {
			return { block: true, reason: "Path is outside the frozen workspace or unavailable." };
		}
	});
}
