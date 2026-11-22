"use client";

// 今日页「今日计划任务」区块（UI 重构阶段 4）：拉取当前活跃计划并复用共享的
// PlanTodayCard（checkbox 完成/跳过/锁徽章/补强高亮/预计分钟），点击任务打开
// PlanTaskDrawer（练习/复习=专注会话、辅导=跳 /tutor，见抽屉内按钮）。
// 无活跃计划 / 加载失败 / 结构为空 → 整块隐藏（今日页以 next-actions 为主视图）。

import { useEffect, useRef, useState } from "react";

import { ErrorBanner } from "@/components/common";
import {
  PlanTodayCard,
  flattenDays,
  mergePlanSnapshot,
  normalizedTaskStatus,
  withTaskStatus,
  withTaskResult,
  type PlanTaskStatus,
} from "@/components/plans/plan-parts";
import {
  PlanTaskDrawer,
  type PlanTaskLocation,
} from "@/components/plans/plan-task-drawer";
import { ApiError, apiJson } from "@/lib/api";
import type {
  PlanDetail,
  PlanListItem,
  PlanToggleResult,
} from "@/lib/types";

type DrawerLocation = Omit<PlanTaskLocation, "planKbId">;

export function TodayPlanSection() {
  const [detail, setDetail] = useState<PlanDetail | null>(null);
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(new Set());
  const pendingStatuses = useRef(new Map<string, PlanTaskStatus>());
  const [openTask, setOpenTask] = useState<DrawerLocation | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const plans = await apiJson<PlanListItem[]>("/api/plans");
        const active = plans.find((plan) => plan.status === "active") ?? null;
        if (!active) {
          if (alive) {
            setDetail(null);
            setOpenTask(null);
            setPendingKeys(new Set());
          }
          return;
        }
        const loaded = await apiJson<PlanDetail>(`/api/plans/${active.id}`);
        if (alive) {
          if (loaded.status === "active") {
            const pending = new Map(pendingStatuses.current);
            setDetail((previous) => mergePlanSnapshot(previous, loaded, pending));
          }
          else {
            setDetail(null);
            setOpenTask(null);
            setPendingKeys(new Set());
          }
        }
      } catch {
        // 静默隐藏：今日页的主视图是 next-actions，计划区块不可用不阻塞
      }
    })();
    return () => {
      alive = false;
    };
  }, [reloadKey]);

  /** 与 /plans 页同一套三态更新：乐观更新 + 失败回滚（见 plans/page.tsx）。 */
  const updateTaskStatus = async (taskId: string, status: PlanTaskStatus) => {
    if (!detail || detail.status !== "active" || pendingStatuses.current.has(taskId)) return;
    const previousTask = flattenDays(detail.structure)
      .flatMap((day) => day.tasks)
      .find((task) => task.task_id === taskId);
    if (!previousTask) return;
    const previousStatus = normalizedTaskStatus(previousTask);
    pendingStatuses.current.set(taskId, status);
    setDetail((previous) => previous ? withTaskStatus(previous, taskId, status) : previous);
    setOpenTask((current) =>
      current?.task.task_id === taskId
        ? { ...current, task: { ...current.task, status } }
        : current,
    );
    setPendingKeys((prev) => new Set(prev).add(taskId));
    setBanner(null);
    try {
      const res = await apiJson<PlanToggleResult>(
        `/api/plans/${detail.id}/tasks/${taskId}/toggle`,
        { body: { status } },
      );
      const hasOtherPending = pendingStatuses.current.size > 1;
      setDetail((prev) => prev && prev.id === detail.id
        ? withTaskResult(prev, res, hasOtherPending) : prev);
      if (res.plan_status !== "active") setOpenTask(null);
    } catch (err) {
      setDetail((prev) =>
        prev && prev.id === detail.id
          ? withTaskStatus(prev, taskId, previousStatus)
          : prev,
      );
      setOpenTask((current) =>
        current?.task.task_id === taskId
          ? { ...current, task: { ...current.task, status: previousStatus } }
          : current,
      );
      setBanner(err instanceof ApiError ? err.message : "任务状态更新失败，已回滚");
    } finally {
      pendingStatuses.current.delete(taskId);
      setPendingKeys((prev) => {
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
      if (pendingStatuses.current.size === 0) setReloadKey((key) => key + 1);
    }
  };

  if (!detail || detail.status !== "active") return null;
  const hasSchedule = flattenDays(detail.structure).length > 0;
  if (!hasSchedule) return null;

  return (
    <section className="space-y-2">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[15px] font-semibold tracking-[-0.01em] text-ink">今日计划任务</h3>
        <span className="min-w-0 truncate text-caption text-ink-muted" title={detail.goal}>
          {detail.goal}
        </span>
      </div>
      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}
      <PlanTodayCard
        structure={detail.structure}
        currentDayNumber={detail.schedule?.current_day}
        onStatus={(taskId, status) => void updateTaskStatus(taskId, status)}
        onTaskOpen={setOpenTask}
        pendingKeys={pendingKeys}
      />
      {openTask && (
        <PlanTaskDrawer
          location={{ ...openTask, planId: detail.id, planKbId: detail.kb_id }}
          onClose={() => setOpenTask(null)}
          onCompleted={(result) => {
            if (result.plan_status !== "active") {
              setDetail(null);
              setOpenTask(null);
              setPendingKeys(new Set());
            } else {
              setReloadKey((key) => key + 1);
            }
          }}
        />
      )}
    </section>
  );
}
