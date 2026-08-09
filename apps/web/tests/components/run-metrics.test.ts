// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { developmentProgressFromGoalGraph, RunMetrics } from "@/components/workbench/run-metrics";
import type { GoalGraphProjection } from "@/lib/contracts";

afterEach(cleanup);

function graphFixture(): GoalGraphProjection {
  return {
    graphId: "graph-1",
    runId: "run-1",
    revision: 1,
    status: "active",
    productOutcome: "Ship the requested page.",
    activeGoalId: "goal-visible-2",
    goals: [
      {
        goalId: "goal-visible-1", title: "Visible one", userVisible: true, dependsOn: [], status: "verified", evidenceCount: 1,
        acceptance: [
          { acceptanceId: "ac-1", title: "Passed", priority: "must", status: "passed" },
          { acceptanceId: "ac-2", title: "Pending", priority: "must", status: "pending" },
        ],
      },
      {
        goalId: "goal-visible-2", title: "Visible two", userVisible: true, dependsOn: [], status: "failed", evidenceCount: 0,
        acceptance: [{ acceptanceId: "ac-3", title: "Blocked", priority: "must", status: "blocked" }],
      },
      {
        goalId: "goal-internal", title: "Internal", userVisible: false, dependsOn: [], status: "verified", evidenceCount: 2,
        acceptance: [
          { acceptanceId: "ac-internal-1", title: "Internal pass one", priority: "must", status: "passed" },
          { acceptanceId: "ac-internal-2", title: "Internal pass two", priority: "must", status: "passed" },
        ],
      },
    ],
  };
}

describe("RunMetrics", () => {
  it("shows unknown context and development values as em dashes", () => {
    render(createElement(RunMetrics, { contextUsage: undefined, goalGraph: null }));

    expect(screen.getAllByText("—")).toHaveLength(2);
    expect(screen.getByRole("progressbar", { name: "Context progress" }).getAttribute("aria-valuetext")).toBe("Unknown");
    expect(screen.getByRole("progressbar", { name: "Development progress" }).getAttribute("aria-valuetext")).toBe("Unknown");
  });

  it("calculates context from real tokens/window and development only from user-visible acceptance", () => {
    render(createElement(RunMetrics, {
      contextUsage: {
        contextTokens: 50_000,
        contextWindow: 200_000,
        boundary: "turn_completed",
        capturedAt: "2026-08-09T09:00:00.000Z",
      },
      goalGraph: graphFixture(),
    }));

    expect(screen.getByRole("progressbar", { name: "Context progress" }).getAttribute("aria-valuenow")).toBe("25");
    expect(screen.getByRole("progressbar", { name: "Development progress" }).getAttribute("aria-valuenow")).toBe("33");
    expect(screen.getByText("turn-boundary snapshot")).toBeTruthy();
    expect(screen.getByText("1/3 acceptance criteria")).toBeTruthy();
  });

  it("falls back to verified user-visible goals when no acceptance criteria exist", () => {
    const graph = graphFixture();
    graph.goals = graph.goals.map((goal) => ({ ...goal, acceptance: [] }));

    expect(developmentProgressFromGoalGraph(graph)).toEqual({
      completed: 1,
      total: 2,
      blocked: 1,
      source: "user-visible goals",
      percent: 50,
    });
  });
});
