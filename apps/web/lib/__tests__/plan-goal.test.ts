import { describe, expect, it } from "vitest";

import {
  PLAN_GOAL_EXAMPLES,
  isPlanGoalReady,
  planGoalReadiness,
} from "@/lib/plan-goal";

describe("plan goal guidance", () => {
  it("requires a topic, observable outcome and success criteria", () => {
    expect(
      isPlanGoalReady({
        goal: "高等数学",
        currentLevel: "",
        targetOutcome: "",
        successCriteria: "",
      }),
    ).toBe(false);
    expect(
      isPlanGoalReady({
        goal: " 高等数学 ",
        currentLevel: "",
        targetOutcome: " 能独立完成极限基础题 ",
        successCriteria: " 10 道题至少正确 8 道 ",
      }),
    ).toBe(true);
  });

  it("reports each field independently and ships complete examples", () => {
    expect(
      planGoalReadiness({
        goal: "导数",
        currentLevel: "会基本极限",
        targetOutcome: "",
        successCriteria: "",
      }),
    ).toEqual({ topic: true, baseline: true, outcome: false, criteria: false });
    expect(PLAN_GOAL_EXAMPLES.length).toBeGreaterThanOrEqual(3);
    expect(PLAN_GOAL_EXAMPLES.every(isPlanGoalReady)).toBe(true);
  });
});
