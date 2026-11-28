import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PlanDetailPanel } from "@/components/plans/plan-detail-panel";
import { TodayPlanSection } from "@/components/plans/today-plan-section";
import { ApiError, apiJson } from "@/lib/api";
import type { PlanDetail, PlanToggleResult } from "@/lib/types";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(), apiJson: vi.fn(),
}));

function plan(id = "plan-a", done: number[] = [], version = 1): PlanDetail {
  return {
    id, goal: `目标 ${id}`, budget_hours: 1, kb_id: "kb-a", kb_name: "课程 A",
    plan_mode: "course_bound", grounding_status: "kb_grounded", version,
    created_at: "2026-11-22T00:00:00Z", status: "active", resources: [],
    structure: { stages: [{ name: "基础", days: [{ day: 1, tasks: [1, 2, 3].map((n) => ({
      task_id: `task-${n}`, title: `任务 ${n}`, kc_names: [], minutes: 20,
      status: done.includes(n) ? "done" as const : "pending" as const,
    })) }] }] },
    progress: { total: 3, done: done.length, skipped: 0, pending: 3 - done.length,
      ratio: done.length / 3, effective_ratio: done.length / 3 },
  };
}

function response(task: number, done: number[], version: number): PlanToggleResult {
  return { task_id: `task-${task}`, status: "done", plan_status: "active", version,
    progress: plan("plan-a", done, version).progress };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const props = { planId: "plan-a", goal: "目标 plan-a", status: "active" as const, budgetHours: 1 };
beforeEach(() => { vi.mocked(apiJson).mockReset(); });

describe("plan editing sessions", () => {
  it.each([false, true])("keeps a successful update when another response is late (failure=%s)", async (failure) => {
    const first = deferred<PlanToggleResult>();
    const second = deferred<PlanToggleResult>();
    let reads = 0;
    vi.mocked(apiJson).mockImplementation((path) => {
      if (path.endsWith("/agents")) return Promise.resolve({ events: [] });
      if (path.endsWith("/adjustments")) return Promise.resolve([]);
      if (path.includes("task-1/toggle")) return first.promise;
      if (path.includes("task-2/toggle")) return second.promise;
      // A stale GET after the newer commit must also be ignored.
      return Promise.resolve(reads++ === 0 ? plan() : failure ? plan("plan-a", [2], 2) : plan("plan-a", [1], 2));
    });
    const onChanged = vi.fn();
    render(<PlanDetailPanel {...props} onChanged={onChanged} />);
    await screen.findByRole("checkbox", { name: "标记完成：任务 1" });
    fireEvent.click(screen.getByRole("checkbox", { name: "标记完成：任务 1" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "标记完成：任务 2" }));
    await act(async () => { second.resolve(response(2, failure ? [2] : [1, 2], failure ? 2 : 3)); });
    expect(screen.getByText("已完成 2 / 3 个任务")).toBeInTheDocument();
    await act(async () => {
      if (failure) first.reject(new ApiError(503, "UNAVAILABLE", "任务状态更新失败"));
      else first.resolve(response(1, [1], 2));
    });
    await waitFor(() => expect(reads).toBe(2));
    expect(screen.getByText(`已完成 ${failure ? 1 : 2} / 3 个任务`)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "取消完成：任务 2" })).toBeChecked();
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("clears the old plan and ignores its pending mutation when switching plans", async () => {
    const toggle = deferred<PlanToggleResult>();
    const replacement = deferred<PlanDetail>();
    vi.mocked(apiJson).mockImplementation((path) => {
      if (path.endsWith("/agents")) return Promise.resolve({ events: [] });
      if (path.endsWith("/adjustments")) return Promise.resolve([]);
      if (path.includes("/toggle")) return toggle.promise;
      if (path === "/api/plans/plan-b") return replacement.promise;
      return Promise.resolve(plan());
    });
    const { rerender } = render(<PlanDetailPanel {...props} />);
    await screen.findByRole("checkbox", { name: "标记完成：任务 1" });
    fireEvent.click(screen.getByRole("checkbox", { name: "标记完成：任务 1" }));
    rerender(<PlanDetailPanel {...props} planId="plan-b" goal="目标 plan-b" />);
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    await act(async () => { replacement.resolve(plan("plan-b")); });
    await act(async () => { toggle.resolve(response(1, [1], 2)); });
    expect(screen.getByText("已完成 0 / 3 个任务")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "标记完成：任务 1" })).not.toBeChecked();
  });

  it("notifies course summaries after an adjustment", async () => {
    vi.mocked(apiJson).mockImplementation((path) => {
      if (path.endsWith("/agents")) return Promise.resolve({ events: [] });
      if (path.endsWith("/adjustments")) return Promise.resolve([]);
      if (path.endsWith("/adjust")) return Promise.resolve({ inserted: [], reason: "无需调整", adjustment_id: null });
      return Promise.resolve(plan());
    });
    const onChanged = vi.fn();
    render(<PlanDetailPanel {...props} onChanged={onChanged} />);
    await screen.findByRole("checkbox", { name: "标记完成：任务 1" });
    fireEvent.click(screen.getByRole("button", { name: "根据学情调整计划" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
  });

  it("shows a proposal and applies it only after the learner confirms", async () => {
    let status = "proposed";
    const proposal = {
      id: "p1", reason: "根据课程测验补强", trigger: "low_score_attempt", at: "2026-11-22T00:00:00Z",
      inserted: [{ task_id: "new", title: "补强单位换算", day: 2, minutes: 10,
        replaces: { id: "r1", title: "任选复习" }, material: { title: "物理讲义", heading: "速度" } }],
    };
    vi.mocked(apiJson).mockImplementation((path) => {
      if (path.endsWith("/agents")) return Promise.resolve({ events: [] });
      if (path.endsWith("/adjustments")) return Promise.resolve([{ ...proposal, status }]);
      if (path.endsWith("/confirm")) {
        status = "applied";
        return Promise.resolve({ ...proposal, status, adjustment_id: proposal.id });
      }
      return Promise.resolve(plan());
    });
    render(<PlanDetailPanel {...props} />);
    const confirm = await screen.findByRole("button", { name: "确认调整计划" });
    expect(screen.getByText(/替换「任选复习」/)).toBeInTheDocument();
    expect(status).toBe("proposed");
    fireEvent.click(confirm);
    await screen.findByText(/已应用 · 1 个任务/);
    expect(screen.queryByRole("button", { name: "确认调整计划" })).not.toBeInTheDocument();
  });

  it("also retains both updates in today's plan card after reversed responses and a stale refresh", async () => {
    const first = deferred<PlanToggleResult>();
    const second = deferred<PlanToggleResult>();
    let reads = 0;
    vi.mocked(apiJson).mockImplementation((path) => {
      if (path === "/api/plans") return Promise.resolve([{ id: "plan-a", status: "active" }]);
      if (path.includes("task-1/toggle")) return first.promise;
      if (path.includes("task-2/toggle")) return second.promise;
      return Promise.resolve(reads++ === 0 ? plan() : plan("plan-a", [1], 2));
    });
    render(<TodayPlanSection />);
    await screen.findByRole("checkbox", { name: "标记完成：任务 1" });
    fireEvent.click(screen.getByRole("checkbox", { name: "标记完成：任务 1" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "标记完成：任务 2" }));
    await act(async () => { second.resolve(response(2, [1, 2], 3)); });
    await act(async () => { first.resolve(response(1, [1], 2)); });
    await waitFor(() => expect(reads).toBe(2));
    expect(screen.getByRole("checkbox", { name: "取消完成：任务 1" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "取消完成：任务 2" })).toBeChecked();
  });
});
