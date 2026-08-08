import type { ArtifactDetail } from "@/lib/contracts";

/**
 * Pure, deterministic ProductSpec / TechnicalSpec JSON-to-Markdown formatting.
 * Backend JSON is the source of truth; unknown fields are never rendered and
 * every rendered value is Markdown-escaped so it can never become raw HTML.
 */

const MAX_OUTPUT_CHARS = 40_000;
const MAX_VALUE_CHARS = 2_000;
const TRUNCATION_MARKER = "\n\n[spec truncated]";

function escapeMarkdown(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/([\\`*_{}[\]()#+.!|>~-])/g, "\\$1");
}

function bounded(value: unknown, fallback = ""): string {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) {
    return fallback;
  }
  const collapsed = text.replace(/\s+/g, " ");
  return collapsed.length <= MAX_VALUE_CHARS ? collapsed : `${collapsed.slice(0, MAX_VALUE_CHARS).trimEnd()}…`;
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function items(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

type Section = { title: string; body: string };

function listLines(entries: unknown[], render: (item: Record<string, unknown>) => string): string {
  return entries
    .map((entry) => render(object(entry)))
    .filter((line) => line.length > 0)
    .join("\n");
}

function productSpecSections(content: Record<string, unknown>): Section[] {
  const sections: Section[] = [];

  const problem = bounded(content.problem);
  if (problem) {
    sections.push({ title: "Problem", body: escapeMarkdown(problem) });
  }

  const targetUsers = items(content.targetUsers).map((entry) => `- ${escapeMarkdown(entry)}`).join("\n");
  if (targetUsers) {
    sections.push({ title: "Target users", body: targetUsers });
  }

  const stories = listLines(items(content.userStories), (item) => {
    const story = bounded(item.story);
    if (!story && !bounded(item.id)) return "";
    const priority = bounded(item.priority);
    return `- ${escapeMarkdown(story || bounded(item.id))}${priority ? ` (${escapeMarkdown(priority)})` : ""}`;
  });
  if (stories) {
    sections.push({ title: "User stories", body: stories });
  }

  const criteria = listLines(items(content.acceptanceCriteria), (item) => {
    const id = bounded(item.id);
    const given = escapeMarkdown(bounded(item.given, "…"));
    const when = escapeMarkdown(bounded(item.when, "…"));
    const then = escapeMarkdown(bounded(item.then, "…"));
    return `- ${id ? `**${escapeMarkdown(id)}** — ` : ""}Given ${given}; when ${when}; then ${then}.`;
  });
  if (criteria) {
    sections.push({ title: "Acceptance criteria", body: criteria });
  }

  const pages = listLines(items(content.pages), (item) => {
    const route = bounded(item.route);
    if (!route) return "";
    const purpose = bounded(item.purpose);
    const keys = items(item.keyElements).map((key) => escapeMarkdown(key)).join(", ");
    return `- ${escapeMarkdown(route)}${purpose ? ` — ${escapeMarkdown(purpose)}` : ""}${keys ? ` (${keys})` : ""}`;
  });
  if (pages) {
    sections.push({ title: "Pages", body: pages });
  }

  const assumptions = items(content.assumptions).map((entry) => `- ${escapeMarkdown(entry)}`).join("\n");
  if (assumptions) {
    sections.push({ title: "Assumptions", body: assumptions });
  }

  const outOfScope = items(content.outOfScope).map((entry) => `- ${escapeMarkdown(entry)}`).join("\n");
  if (outOfScope) {
    sections.push({ title: "Out of scope", body: outOfScope });
  }

  return sections;
}

function technicalSpecSections(content: Record<string, unknown>): Section[] {
  const sections: Section[] = [];

  const framework = bounded(content.framework);
  if (framework) {
    sections.push({ title: "Framework", body: escapeMarkdown(framework) });
  }

  const capabilities = items(content.starterCapabilities).map((entry) => `- ${escapeMarkdown(entry)}`).join("\n");
  if (capabilities) {
    sections.push({ title: "Starter capabilities", body: capabilities });
  }

  const routes = listLines(items(content.routes), (item) => {
    const path = bounded(item.path);
    if (!path) return "";
    const rendering = bounded(item.rendering);
    const description = bounded(item.description);
    return `- ${escapeMarkdown(path)}${rendering ? ` — ${escapeMarkdown(rendering)}` : ""}${description ? `: ${escapeMarkdown(description)}` : ""}`;
  });
  if (routes) {
    sections.push({ title: "Routes", body: routes });
  }

  const components = listLines(items(content.components), (item) => {
    const name = bounded(item.name);
    if (!name) return "";
    const responsibility = bounded(item.responsibility);
    return `- ${escapeMarkdown(name)}${responsibility ? ` — ${escapeMarkdown(responsibility)}` : ""}`;
  });
  if (components) {
    sections.push({ title: "Components", body: components });
  }

  const decisions = listLines(items(content.componentDecisions), (item) => {
    const component = bounded(item.component);
    if (!component) return "";
    const strategy = bounded(item.strategy);
    const rationale = bounded(item.rationale);
    return `- ${escapeMarkdown(component)}${strategy ? ` (${escapeMarkdown(strategy)})` : ""}${rationale ? ` — ${escapeMarkdown(rationale)}` : ""}`;
  });
  if (decisions) {
    sections.push({ title: "Component decisions", body: decisions });
  }

  const surfaces = listLines(items(content.featureSurfaces), (item) => {
    const name = bounded(item.componentName);
    if (!name) return "";
    const file = bounded(item.compositionFile);
    return `- ${escapeMarkdown(name)}${file ? ` — ${escapeMarkdown(file)}` : ""}`;
  });
  if (surfaces) {
    sections.push({ title: "Feature surfaces", body: surfaces });
  }

  const stateModels = listLines(items(content.stateModel), (item) => {
    const name = bounded(item.name);
    if (!name) return "";
    const owner = bounded(item.owner);
    const persistence = bounded(item.persistence);
    return `- ${escapeMarkdown(name)}${owner ? ` (${escapeMarkdown(owner)})` : ""}${persistence ? ` — ${escapeMarkdown(persistence)}` : ""}`;
  });
  if (stateModels) {
    sections.push({ title: "State model", body: stateModels });
  }

  const domains = listLines(items(content.persistentStateDomains), (item) => {
    const domain = bounded(item.domain);
    if (!domain) return "";
    const store = bounded(item.actionsStoreFile);
    return `- ${escapeMarkdown(domain)}${store ? ` → ${escapeMarkdown(store)}` : ""}`;
  });
  if (domains) {
    sections.push({ title: "Persistent state domains", body: domains });
  }

  const aggregation = object(content.stateAggregation);
  const aggregationPath = bounded(aggregation.filePath);
  if (aggregationPath) {
    const responsibilities = items(aggregation.responsibilities).map((entry) => escapeMarkdown(entry)).join(", ");
    sections.push({
      title: "State aggregation",
      body: `- ${escapeMarkdown(aggregationPath)}${responsibilities ? ` — ${responsibilities}` : ""}`,
    });
  }

  const dependencies = listLines(items(content.dependencies), (item) => {
    const name = bounded(item.name);
    if (!name) return "";
    const reason = bounded(item.reason);
    return `- ${escapeMarkdown(name)}${reason ? ` — ${escapeMarkdown(reason)}` : ""}`;
  });
  if (dependencies) {
    sections.push({ title: "Dependencies", body: dependencies });
  }

  const filePlan = listLines(items(content.filePlan), (item) => {
    const path = bounded(item.path);
    if (!path) return "";
    const operation = bounded(item.operation);
    const reason = bounded(item.reason);
    return `- ${operation ? `${escapeMarkdown(operation)} ` : ""}${escapeMarkdown(path)}${reason ? ` — ${escapeMarkdown(reason)}` : ""}`;
  });
  if (filePlan) {
    sections.push({ title: "File plan", body: filePlan });
  }

  const testPlan = listLines(items(content.testPlan), (item) => {
    const acceptanceId = bounded(item.acceptanceId);
    if (!acceptanceId) return "";
    const method = bounded(item.method);
    const steps = items(item.steps).map((step) => escapeMarkdown(step)).join("; ");
    return `- ${escapeMarkdown(acceptanceId)}${method ? ` (${escapeMarkdown(method)})` : ""}${steps ? ` — ${steps}` : ""}`;
  });
  if (testPlan) {
    sections.push({ title: "Test plan", body: testPlan });
  }

  const risks = items(content.risks).map((entry) => `- ${escapeMarkdown(entry)}`).join("\n");
  if (risks) {
    sections.push({ title: "Risks", body: risks });
  }

  return sections;
}

function directArtifactSections(detail: ArtifactDetail): Section[] {
  const content = detail.content;
  if (detail.kind === "run_input") {
    const requirement = bounded(content.requirement);
    const capabilities = items(content.starterCapabilities).map((entry) => `- ${escapeMarkdown(entry)}`).join("\n");
    return [
      ...(requirement ? [{ title: "Requirement", body: escapeMarkdown(requirement) }] : []),
      ...(capabilities ? [{ title: "Golden starter capabilities", body: capabilities }] : []),
    ];
  }
  if (detail.kind === "build_plan") {
    const summary = bounded(content.summary);
    const routes = items(content.routes).map((entry) => `- ${escapeMarkdown(entry)}`).join("\n");
    const files = listLines(items(content.files), (item) => {
      const path = bounded(item.path);
      if (!path) return "";
      const purpose = bounded(item.purpose);
      const acceptanceIds = items(item.acceptanceIds).map((entry) => escapeMarkdown(entry)).join(", ");
      return `- ${escapeMarkdown(path)}${purpose ? ` — ${escapeMarkdown(purpose)}` : ""}${acceptanceIds ? ` (${acceptanceIds})` : ""}`;
    });
    return [
      ...(summary ? [{ title: "Outcome", body: escapeMarkdown(summary) }] : []),
      ...(routes ? [{ title: "Routes", body: routes }] : []),
      ...(files ? [{ title: "Implementation files", body: files }] : []),
    ];
  }
  if (detail.kind === "acceptance_contract") {
    const criteria = listLines(items(content.criteria), (item) => {
      const id = bounded(item.id);
      if (!id) return "";
      return `- **${escapeMarkdown(id)}** — ${escapeMarkdown(bounded(item.title, bounded(item.then, "Acceptance workflow")))}`;
    });
    const tests = listLines(items(content.tests), (item) => {
      const acceptanceId = bounded(item.acceptanceId);
      const title = bounded(item.title);
      return acceptanceId ? `- ${escapeMarkdown(acceptanceId)} → ${escapeMarkdown(title || "Playwright workflow")}` : "";
    });
    return [
      ...(criteria ? [{ title: "Frozen criteria", body: criteria }] : []),
      ...(tests ? [{ title: "FOMO-owned tests", body: tests }] : []),
    ];
  }
  const gates = listLines(items(content.gates), (item) => {
    const gate = bounded(item.gate);
    if (!gate) return "";
    const status = bounded(item.status);
    const summary = bounded(item.summary);
    return `- ${escapeMarkdown(gate)}${status ? ` — ${escapeMarkdown(status)}` : ""}${summary ? `: ${escapeMarkdown(summary)}` : ""}`;
  });
  return gates ? [{ title: "Deterministic gates", body: gates }] : [];
}

export function formatArtifactDetail(detail: ArtifactDetail): string {
  const title = bounded(detail.title, detail.kind.replace("_", " "));
  const sections = detail.kind === "product_spec"
    ? productSpecSections(detail.content)
    : detail.kind === "technical_spec"
      ? technicalSpecSections(detail.content)
      : directArtifactSections(detail);
  const lines: string[] = [`# ${escapeMarkdown(title)}`];
  for (const section of sections) {
    lines.push("", `## ${escapeMarkdown(section.title)}`, "", section.body.trim());
  }
  const output = lines.join("\n");
  if (output.length <= MAX_OUTPUT_CHARS) {
    return output;
  }
  // The truncation marker counts toward the hard bound: the final string is
  // at most MAX_OUTPUT_CHARS characters including the marker.
  return `${output.slice(0, MAX_OUTPUT_CHARS - TRUNCATION_MARKER.length).trimEnd()}${TRUNCATION_MARKER}`;
}
