import { describe, expect, it } from "vitest";

import { projectStatusLabel } from "@/lib/project-status";

describe("project status display", () => {
  it("prefers a loaded terminal run and otherwise preserves idle", () => {
    expect(projectStatusLabel({ status: "idle" }, "failed")).toBe("failed");
    expect(projectStatusLabel({ status: "idle" })).toBe("idle");
  });
});
