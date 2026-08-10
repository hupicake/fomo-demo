// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { TaskSummary } from "@/components/workbench/goal-graph-panel";
import type { GoalGraphProjection, GoalProjection } from "@/lib/contracts";

afterEach(cleanup);

const statusFixtures: GoalProjection["status"][] = ["pending", "active", "claimed", "verified", "failed", "superseded"];

function graphFixture(): GoalGraphProjection {
  return {
    graphId: "graph-library",
    runId: "run-library",
    revision: 3,
    status: "active",
    productOutcome: "Readers can find and borrow available books.",
    activeGoalId: "G-2",
    goals: statusFixtures.map((status, index) => ({
      goalId: `G-${index + 1}`,
      title: `${status} goal`,
      userVisible: index !== 5,
      dependsOn: index === 0 ? [] : [`G-${index}`],
      status,
      checkpointId: status === "verified" ? "checkpoint-verified" : undefined,
      claimedAt: status === "claimed" ? "2026-08-09T10:00:00.000Z" : undefined,
      verifiedAt: status === "verified" ? "2026-08-09T10:05:00.000Z" : undefined,
      acceptance: [{
        acceptanceId: `AC-${index + 1}`,
        title: `${status} acceptance`,
        priority: index % 2 === 0 ? "must" : "should",
        status: status === "verified" ? "passed" : status === "failed" ? "failed" : "pending",
      }],
      evidenceCount: index,
    })),
  };
}

function independentReadyGraphFixture(): GoalGraphProjection {
  const goal = (goalId: string, title: string, dependsOn: string[] = []): GoalProjection => ({
    goalId,
    title,
    userVisible: true,
    dependsOn,
    status: "pending",
    acceptance: [{ acceptanceId: `AC-${goalId}`, title: `${title} works`, priority: "must", status: "pending" }],
    evidenceCount: 0,
  });
  return {
    graphId: "graph-ready",
    runId: "run-ready",
    revision: 1,
    status: "active",
    productOutcome: "Readers can use the complete library workflow.",
    activeGoalId: null,
    goals: [
      goal("G-search", "Build search"),
      goal("G-loans", "Build loans"),
      goal("G-admin", "Build admin", ["G-search"]),
    ],
  };
}

describe("TaskSummary", () => {
  it("keeps a compact waiting state for runs without a goal projection", () => {
    render(createElement(TaskSummary, { graph: null }));

    expect(screen.getByRole("region", { name: "当前任务" })).toBeTruthy();
    expect(screen.getByText("交付计划就绪后会显示在这里。")).toBeTruthy();
    expect(screen.queryByText(/GoalGraph projection/i)).toBeNull();
  });

  it("keeps the current task fixed in the summary and exposes plan details by keyboard", async () => {
    const user = userEvent.setup();
    render(createElement(TaskSummary, { graph: graphFixture() }));

    expect(screen.getByText("active goal")).toBeTruthy();
    expect(screen.getByText(/currently executed sequentially/i)).toBeTruthy();

    const details = screen.getByRole("button", { name: "Plan details" });
    details.focus();
    expect(document.activeElement).toBe(details);
    await user.keyboard("{Enter}");
    expect(details.getAttribute("aria-expanded")).toBe("true");
    expect(await screen.findByText("完整交付计划")).toBeTruthy();
    expect(screen.getByText("Readers can find and borrow available books.")).toBeTruthy();
    expect(screen.getByText("1/5 acceptance criteria complete")).toBeTruthy();
    expect(screen.getByText("1 blocked")).toBeTruthy();

    expect(screen.queryByText("revision 3")).toBeNull();
    expect(screen.queryByText("checkpoint-verified")).toBeNull();
    expect(screen.queryByText("G-1")).toBeNull();
  });

  it("marks independent pending goals Ready while stating that execution is sequential, not concurrent", async () => {
    const user = userEvent.setup();
    render(createElement(TaskSummary, { graph: independentReadyGraphFixture() }));

    expect(screen.getByText("2 Ready · currently executed sequentially")).toBeTruthy();
    expect(screen.getByText("Build search")).toBeTruthy();
    expect(screen.getByText("Ready", { exact: true })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Plan details" }));
    expect(await screen.findByText(/2 goals are Ready/)).toBeTruthy();
    expect(screen.getByText(/not concurrent execution/i)).toBeTruthy();
    expect(screen.getAllByText("Ready", { exact: true }).length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("Waiting", { exact: true })).toBeTruthy();
  });

  it("renders an empty server graph as a concise no-active-goal state", () => {
    render(createElement(TaskSummary, { graph: { ...graphFixture(), goals: [], activeGoalId: null } }));

    expect(screen.getByText("No active goal")).toBeTruthy();
    expect(screen.getByText("Current plan executes goals sequentially")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Plan details" })).toBeTruthy();
  });
});
