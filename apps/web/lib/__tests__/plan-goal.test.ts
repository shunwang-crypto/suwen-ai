import { describe, expect, it } from "vitest";

import {
  PLAN_GOAL_EXAMPLES,
  isPlanGoalReady,
  parsePlanIntent,
  planGoalReadiness,
} from "@/lib/plan-goal";

describe("plan goal guidance", () => {
  it("starts with a single user-owned goal sentence", () => {
    expect(isPlanGoalReady({ goal: "高等数学", currentLevel: "", targetOutcome: "", successCriteria: "" })).toBe(true);
    expect(isPlanGoalReady({ goal: " 高等数学 ", currentLevel: "", targetOutcome: "", successCriteria: "" })).toBe(true);
  });

  it("extracts only explicit deadline and daily time, leaving missing facts to confirm", () => {
    expect(parsePlanIntent("我想在 9月20日 达到能独立做极限题，每天 1.5 小时", new Date(2026, 7, 9))).toMatchObject({
      deadline: "2026-09-20",
      dailyHours: 1.5,
      totalDays: null,
      missing: [],
    });
    expect(parsePlanIntent("14天内掌握导数", new Date(2026, 7, 9))).toMatchObject({
      deadline: null,
      totalDays: 14,
      missing: ["daily_hours"],
    });
    expect(parsePlanIntent("两周内掌握导数，每天 1 小时", new Date(2026, 7, 9))).toMatchObject({
      deadline: null,
      totalDays: 14,
      dailyHours: 1,
      missing: [],
    });
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
