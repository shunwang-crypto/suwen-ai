import { describe, expect, it } from "vitest";

import { buildTodayItems, taskExecutionHref, type FlatPlanTask } from "@/app/today/today-plan";

function task(task_key: string, day: number, title: string, extra: Partial<FlatPlanTask> = {}): FlatPlanTask {
  return {
    task_key,
    day,
    stage: "阶段一",
    title,
    minutes: 20,
    kc_names: ["函数"],
    action_type: "practice",
    objective: `掌握${title}`,
    ...extra,
  };
}

describe("server-owned today plan buckets", () => {
  it("orders due reviews, today tasks, overdue tasks, then one recent completion", () => {
    const today = task("today", 3, "今天任务");
    const overdue = task("overdue", 1, "逾期任务");
    const future = task("future", 4, "未来任务");
    const completed = task("done", 2, "最近完成", { done: true });
    const result = buildTodayItems(
      [today, overdue, future, completed],
      { current_day: 3, today_tasks: ["today"], overdue_tasks: ["overdue"], upcoming_tasks: ["future"] },
      [{ error_item_id: "e1", question: { stem: "到期错题" } } as never],
      "11111111-1111-4111-8111-111111111111",
    );

    expect(result.todayTasks.map((item) => item.title)).toEqual(["今天任务"]);
    expect(result.overdueTasks.map((item) => item.title)).toEqual(["逾期任务"]);
    expect(result.todos.map((item) => item.title)).toEqual(["到期错题", "今天任务", "逾期任务", "最近完成"]);
    expect(result.todos.map((item) => item.dueLabel)).toEqual(["今日到期", "计划今日", "逾期", undefined]);
    expect(result.todos.some((item) => item.title === "未来任务")).toBe(false);
  });

  it("keeps all unfinished tasks overdue after the schedule has elapsed", () => {
    const result = buildTodayItems(
      [task("old", 1, "已过期"), task("also-old", 2, "也过期"), task("future", 4, "未来")],
      { current_day: 5, today_tasks: [], overdue_tasks: ["old", "also-old"], upcoming_tasks: ["future"] },
      [],
    );
    expect(result.todayTasks).toHaveLength(0);
    expect(result.overdueTasks.map((item) => item.task_key)).toEqual(["old", "also-old"]);
    expect(result.todos.map((item) => item.dueLabel)).toEqual(["逾期", "逾期"]);
  });
});

describe("task execution links", () => {
  const base = task("t", 1, "极限练习", { practice_count: 12, difficulty: "easy" });
  it("uses real page parameters for practice, tutor, errors, and resource fallback", () => {
    expect(taskExecutionHref(base, "11111111-1111-4111-8111-111111111111")).toContain("/quiz?topic=%E5%87%BD%E6%95%B0&kb=11111111-1111-4111-8111-111111111111&count=12&difficulty=easy");
    expect(taskExecutionHref({ ...base, action_type: "tutor" }, null)).toBe("/tutor?problem=%E6%8E%8C%E6%8F%A1%E6%9E%81%E9%99%90%E7%BB%83%E4%B9%A0");
    expect(taskExecutionHref({ ...base, action_type: "review_errors" }, "11111111-1111-4111-8111-111111111111")).toBe("/errors?kb_id=11111111-1111-4111-8111-111111111111");
    expect(taskExecutionHref({ ...base, action_type: "read", resource: { title: "公开资料", url: "https://example.com/lesson" } }, null)).toBe("https://example.com/lesson");
  });
});
