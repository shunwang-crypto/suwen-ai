"use client";

// 计划详情面板（UI 重构阶段 5 自 /plans 右列抽离的共享组件）：分段进度条 + 学情调整
// （结果横幅/补强高亮）+ 目标回看 + KC 依赖图 + 日历（含任务抽屉）+ 调整历史。
// /plans 聚合壳与 /courses/[kbId] 计划 tab 渲染同一份，交互逻辑单处维护。

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

import { ModelMarkdown } from "@/components/chat/model-markdown";
import {
  Card,
  EmptyState,
  ErrorBanner,
  SkeletonList,
  Spinner,
  fmtDateTime,
  fmtPct,
} from "@/components/common";
import { KcDependencyPath } from "@/components/plans/kc-dependency-path";
import {
  PlanCalendar,
  STAGE_COLORS,
  TRIGGER_LABELS,
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
import { MathText } from "@/lib/katex";
import { toast } from "@/lib/toast";
import type {
  PlanAdjustResult,
  PlanAdjustmentItem,
  PlanDetail,
  PlanMode,
  PlanToggleResult,
} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

type DrawerLocation = PlanTaskLocation;

/**
 * 「重新生成」预填载荷（T1-1b）：把一份计划的目标与参数回填到 /plans 新建表单，
 * 交由既有创建流（POST /api/plans，finalize 会自动归档当前 active）重跑一遍。
 */
export interface PlanRegeneratePrefill {
  goal: string;
  planMode: PlanMode;
  kbId: string | null;
  hours: number;
  totalDays: number;
  targetOutcome: string;
  successCriteria: string;
  currentLevel: string;
}

type PlanDetailPanelProps = {
  planId: string;
  goal: string;
  status: "active" | "completed" | "archived";
  budgetHours: number;
  /** 计划列表刷新回调（任务完成/调整后可让外层同步进度摘要） */
  onChanged?: () => void;
  /**
   * 「重新生成」回调：仅 /plans 聚合壳提供（那里有创建表单）；/courses 计划 tab 不传 → 按钮不渲染。
   * 只负责回填，真正重生成复用 POST /api/plans 创建流。
   */
  onRegenerate?: (prefill: PlanRegeneratePrefill) => void;
};

export function PlanDetailPanel(props: PlanDetailPanelProps) {
  // A new plan owns a new editing session, including its drawers and requests.
  return <PlanDetailContent key={props.planId} {...props} />;
}

function PlanDetailContent({
  planId,
  goal,
  status,
  budgetHours,
  onChanged,
  onRegenerate,
}: PlanDetailPanelProps) {
  const [detailKey, setDetailKey] = useState(0);
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(new Set());
  const pendingStatuses = useRef(new Map<string, PlanTaskStatus>());
  const [openTask, setOpenTask] = useState<DrawerLocation | null>(null);
  const [taskBanner, setTaskBanner] = useState<string | null>(null);
  const [adjusting, setAdjusting] = useState(false);
  const [adjustResult, setAdjustResult] = useState<PlanAdjustResult | null>(null);
  const [highlightKeys, setHighlightKeys] = useState<Set<string>>(new Set());
  const [archiving, setArchiving] = useState(false);
  const [reAnchoring, setReAnchoring] = useState(false);

  const detailRes = useApiResource<PlanDetail>(`/api/plans/${planId}`, {
    errorMessage: "计划详情加载失败",
    deps: [planId, detailKey],
  });
  const adjustmentsRes = useApiResource<PlanAdjustmentItem[]>(
    `/api/plans/${planId}/adjustments`,
    { deps: [planId, detailKey] },
  );

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 详情/调整历史的单向同步, 无纯派生替代 */
  const [detail, setDetail] = useState<PlanDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  useEffect(() => {
    const incoming = detailRes.data;
    if (incoming) {
      const pending = new Map(pendingStatuses.current);
      setDetail((previous) => mergePlanSnapshot(previous, incoming, pending));
    }
    setDetailError(detailRes.error);
  }, [detailRes.data, detailRes.error]);

  const [adjustments, setAdjustments] = useState<PlanAdjustmentItem[]>([]);
  useEffect(() => {
    setAdjustments(adjustmentsRes.data ?? []);
  }, [adjustmentsRes.data]);
  /* eslint-enable react-hooks/set-state-in-effect */

  /** 任务三态更新：乐观更新；失败只恢复当前任务（与今日页同一模式）。 */
  const updateTaskStatus = async (taskId: string, next: PlanTaskStatus) => {
    if (!detail || detail.status !== "active" || pendingStatuses.current.has(taskId)) return;
    const snapshot = detail;
    const previousTask = flattenDays(detail.structure)
      .flatMap((day) => day.tasks)
      .find((task) => task.task_id === taskId);
    if (!previousTask) return;
    const previousStatus = normalizedTaskStatus(previousTask);
    pendingStatuses.current.set(taskId, next);
    setDetail((previous) => previous ? withTaskStatus(previous, taskId, next) : previous);
    setOpenTask((current) =>
      current?.task.task_id === taskId
        ? { ...current, task: { ...current.task, status: next } }
        : current,
    );
    setPendingKeys((prev) => new Set(prev).add(taskId));
    setTaskBanner(null);
    try {
      const res = await apiJson<PlanToggleResult>(
        `/api/plans/${snapshot.id}/tasks/${taskId}/toggle`,
        { body: { status: next } },
      );
      // Server commit order, rather than response arrival order, owns progress.
      const hasOtherPending = pendingStatuses.current.size > 1;
      setDetail((prev) =>
        prev && prev.id === snapshot.id
          ? withTaskResult(prev, res, hasOtherPending)
          : prev,
      );
    } catch (err) {
      setDetail((prev) =>
        prev && prev.id === snapshot.id
          ? withTaskStatus(prev, taskId, previousStatus)
          : prev,
      );
      setOpenTask((current) =>
        current?.task.task_id === taskId
          ? { ...current, task: { ...current.task, status: previousStatus } }
          : current,
      );
      setTaskBanner(err instanceof ApiError ? err.message : "任务状态更新失败，已回滚");
    } finally {
      pendingStatuses.current.delete(taskId);
      setPendingKeys((prev) => {
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
      if (pendingStatuses.current.size === 0) {
        setDetailKey((key) => key + 1);
        onChanged?.();
      }
    }
  };

  /** 根据学情调整计划：插入补强任务 → 重拉详情并高亮新任务。 */
  const runAdjust = async () => {
    if (!detail || detail.status !== "active" || adjusting) return;
    setAdjusting(true);
    setTaskBanner(null);
    try {
      const res = await apiJson<PlanAdjustResult>(
        `/api/plans/${detail.id}/adjust`,
        { method: "POST", body: {} },
      );
      setAdjustResult(res);
      setHighlightKeys(new Set(res.inserted.map((t) => t.task_id)));
      setDetailKey((k) => k + 1);
      onChanged?.();
    } catch (err) {
      setTaskBanner(err instanceof ApiError ? err.message : "计划调整失败，请重试");
    } finally {
      setAdjusting(false);
    }
  };

  /** 归档计划（软删，只读留存）；仅 active 可归档，走后端 archive 端点。 */
  const archivePlan = async () => {
    if (!detail || detail.status !== "active" || archiving) return;
    if (
      typeof window !== "undefined" &&
      !window.confirm("归档后这份计划会变成只读，不能再勾选任务或调整。确定归档吗？")
    ) {
      return;
    }
    setArchiving(true);
    setTaskBanner(null);
    try {
      await apiJson(`/api/plans/${detail.id}/archive`, { method: "POST", body: {} });
      toast.success("计划已归档");
      onChanged?.();
      setDetailKey((k) => k + 1);
    } catch (err) {
      setTaskBanner(err instanceof ApiError ? err.message : "归档失败，请重试");
    } finally {
      setArchiving(false);
    }
  };

  /** 把已能匹配本库正式 KC 的临时概念锚定为正式知识点（T1-1c，走后端 re-anchor）。 */
  const reAnchor = async () => {
    if (!detail || detail.status !== "active" || reAnchoring) return;
    setReAnchoring(true);
    setTaskBanner(null);
    try {
      const res = await apiJson<{ anchored: number }>(
        `/api/plans/${detail.id}/re-anchor`,
        { method: "POST", body: {} },
      );
      if (res.anchored > 0) {
        toast.success(`已把 ${res.anchored} 个临时概念锚定为正式知识点`);
      } else {
        toast.info("暂时没有可锚定的概念，去资料里补充或增强后再试");
      }
      onChanged?.();
      setDetailKey((k) => k + 1);
    } catch (err) {
      setTaskBanner(err instanceof ApiError ? err.message : "重新锚定失败，请重试");
    } finally {
      setReAnchoring(false);
    }
  };

  /** 「重新生成」：用本计划参数回填新建表单，交给外层创建流（复用 POST /api/plans）。 */
  const handleRegenerate = () => {
    if (!detail || !onRegenerate) return;
    const totalDays = detail.structure.stages.reduce(
      (sum, stage) => sum + (stage.days?.length ?? 0),
      0,
    );
    const ga = detail.structure.goal_analysis;
    onRegenerate({
      goal: detail.goal,
      planMode: detail.plan_mode,
      kbId: detail.kb_id,
      hours: detail.budget_hours,
      totalDays,
      targetOutcome: ga?.outcomes?.join("；") ?? "",
      successCriteria: ga?.success_criteria?.join("；") ?? "",
      currentLevel: "",
    });
  };

  const openDependencyTask = (taskId: string) => {
    if (!detail) return;
    for (const stage of detail.structure.stages) {
      for (const day of stage.days ?? []) {
        const task = (day.tasks ?? []).find((item) => item.task_id === taskId);
        if (task) {
          setOpenTask({
            task,
            stageName: stage.name,
            day: day.day,
            planId: detail.id,
            planKbId: detail.kb_id,
            readOnly: detail.status !== "active",
          });
          return;
        }
      }
    }
  };

  const view = detail;
  const currentStatus = view?.status ?? status;

  // 逾期概览（T1-2）：只统计仍待办的逾期任务，避免把已完成的旧任务误报为"逾期"。
  // 后端 schedule.overdue_tasks 仅按日期分桶（day < current_day），不看任务状态，故此处再过滤。
  const overdueInfo = (() => {
    if (!view || view.status !== "active" || !view.schedule) return null;
    const overdueIds = view.schedule.overdue_tasks ?? [];
    if (overdueIds.length === 0) return null;
    const currentDay = view.schedule.current_day ?? 0;
    const dayByTask = new Map<string, number>();
    const statusByTask = new Map<string, PlanTaskStatus>();
    for (const flatDay of flattenDays(view.structure)) {
      for (const task of flatDay.tasks) {
        dayByTask.set(task.task_id, flatDay.day);
        statusByTask.set(task.task_id, normalizedTaskStatus(task));
      }
    }
    const pending = overdueIds.filter((id) => statusByTask.get(id) === "pending");
    if (pending.length === 0) return null;
    let maxBehind = 0;
    for (const id of pending) {
      const day = dayByTask.get(id);
      if (day != null && currentDay - day > maxBehind) maxBehind = currentDay - day;
    }
    return { count: pending.length, maxBehind };
  })();

  return (
    <>
      <Card
        title={<span className="block max-w-100 truncate">{goal}</span>}
        extra={
          <span className="flex flex-wrap items-center justify-end gap-1.5 text-caption text-ink-muted">
            {view && <span className={`rounded-full px-2 py-0.5 font-medium ${view.plan_mode === "course_bound" ? "bg-success-50 text-success-700" : "bg-warning-50 text-warning-700"}`}>{view.plan_mode === "course_bound" ? "课程资料计划" : "目标起步计划"}</span>}
            <span>{currentStatus === "active" ? "进行中 · " : currentStatus === "completed" ? "已完成 · " : "已归档 · "}每日 {budgetHours} 小时</span>
          </span>
        }
      >
        {view?.plan_mode === "goal_only" && (
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warning-200 bg-warning-50 px-3.5 py-3">
            <div>
              <p className="text-body-sm font-semibold text-warning-800">这是一份目标起步计划</p>
              <p className="mt-1 text-caption leading-5 text-warning-700">当前概念和练习用于校准起点，不进入正式知识图谱，也不更新课程掌握度。上传资料后可新建一份课程资料计划。</p>
            </div>
            <Link href="/courses" className="rounded-lg border border-warning-300 bg-white px-3 py-2 text-body-sm font-medium text-warning-800 hover:bg-warning-100">上传资料后校准计划</Link>
          </div>
        )}
        {view?.status === "completed" && (
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-3.5 py-3">
            <div>
              <p className="text-body-sm font-semibold text-emerald-800">这份计划已完成 🎉</p>
              <p className="mt-1 text-caption leading-5 text-emerald-700">
                去成长档案看看掌握度的变化，或用它作模板再开一份新计划继续精进。
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Link
                href="/growth"
                className="rounded-control border border-emerald-300 bg-white px-3 py-2 text-body-sm font-medium text-emerald-800 hover:bg-emerald-100"
              >
                查看成长档案
              </Link>
              {onRegenerate && (
                <button
                  type="button"
                  onClick={handleRegenerate}
                  className="rounded-control border border-emerald-300 bg-white px-3 py-2 text-body-sm font-medium text-emerald-800 hover:bg-emerald-100"
                >
                  用它作模板新建
                </button>
              )}
            </div>
          </div>
        )}
        {overdueInfo && (
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-3.5 py-3">
            <div className="min-w-0">
              <p className="text-body-sm font-semibold text-amber-800">
                有 {overdueInfo.count} 个任务已逾期
              </p>
              <p className="mt-1 text-caption leading-5 text-amber-700">
                {overdueInfo.maxBehind > 0
                  ? `最早的一个已过去 ${overdueInfo.maxBehind} 天。`
                  : "已到计划安排的日期。"}
                {view?.plan_mode === "course_bound"
                  ? "可点「根据学情调整计划」按最新学情补强，或直接从今天的任务继续。"
                  : "从今天的任务继续即可，节奏可在新建计划时调整。"}
              </p>
            </div>
          </div>
        )}
        {/* 计划头部：任务粒度进度（分段式，每阶段一段）+ 学情调整入口 */}
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <div className="min-w-40 flex-1">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-caption font-medium text-ink-muted">
                已完成 {view?.progress.done ?? 0} / {view?.progress.total ?? 0} 个任务
                {(view?.progress.skipped ?? 0) > 0 &&
                  ` · 跳过 ${view?.progress.skipped ?? 0} 个`}
              </span>
              <span className="font-mono text-caption text-ink-muted">
                {fmtPct(view?.progress.effective_ratio ?? view?.progress.ratio ?? 0)}
              </span>
            </div>
            {view && view.structure.stages.length > 0 ? (
              <div className="mt-1 flex h-2 gap-0.5 overflow-hidden rounded-full">
                {view.structure.stages.map((stage, si) => {
                  const tasks = (stage.days ?? []).flatMap((d) => d.tasks ?? []);
                  const doneCount = tasks.filter(
                    (task) => normalizedTaskStatus(task) === "done",
                  ).length;
                  const skippedCount = tasks.filter(
                    (task) => normalizedTaskStatus(task) === "skipped",
                  ).length;
                  const effectiveTotal = tasks.length - skippedCount;
                  const colorBar = STAGE_COLORS[si % STAGE_COLORS.length].bar;
                  return (
                    <div
                      key={si}
                      className="h-full min-w-2 bg-surface-muted"
                      style={{ flexGrow: Math.max(tasks.length, 1) }}
                      title={`${stage.name}：完成 ${doneCount} 个，跳过 ${skippedCount} 个`}
                    >
                      <div
                        className={`h-full ${colorBar} transition-all duration-300`}
                        style={{
                          width: `${
                            effectiveTotal
                              ? Math.round((doneCount / effectiveTotal) * 100)
                              : tasks.length
                                ? 100
                                : 0
                          }%`,
                        }}
                      />
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="mt-1 h-2 rounded-full bg-surface-muted">
                <div
                  className="h-full rounded-full bg-indigo-500 transition-all duration-300"
                  style={{
                    width: `${Math.round((view?.progress.effective_ratio ?? view?.progress.ratio ?? 0) * 100)}%`,
                  }}
                />
              </div>
            )}
          </div>
          <button
            type="button"
            disabled={!view || view.status !== "active" || adjusting || view.plan_mode !== "course_bound"}
            onClick={() => void runAdjust()}
            title={view?.plan_mode === "goal_only" ? "目标起步计划不读取其他课程掌握度自动调整" : "按当前课程的真实学习证据插入补强任务"}
            className="flex items-center gap-1.5 rounded-xl border border-amber-200 bg-amber-50 px-3.5 py-2 text-sm font-medium text-amber-700 hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {adjusting && <Spinner className="h-3.5 w-3.5" />}
            {adjusting ? "正在调整…" : "根据学情调整计划"}
          </button>
          {onRegenerate && view && (
            <button
              type="button"
              onClick={handleRegenerate}
              title="用这份计划的目标和参数预填新建表单，重新生成一份（旧计划会自动归档留存）"
              className="flex items-center gap-1.5 rounded-xl border border-brand-200 bg-brand-50 px-3.5 py-2 text-sm font-medium text-brand-700 hover:bg-brand-100"
            >
              重新生成
            </button>
          )}
          {view?.status === "active" && (
            <button
              type="button"
              disabled={archiving}
              onClick={() => void archivePlan()}
              title="归档后计划变为只读，可在计划列表按「已归档」查看"
              className="flex items-center gap-1.5 rounded-xl border border-line-strong bg-white px-3.5 py-2 text-sm font-medium text-ink-secondary hover:bg-surface-muted disabled:cursor-not-allowed disabled:opacity-50"
            >
              {archiving && <Spinner className="h-3.5 w-3.5" />}
              {archiving ? "归档中…" : "归档计划"}
            </button>
          )}
        </div>

        {adjustResult && (
          <div
            className={`mb-3 rounded-xl border px-3.5 py-2.5 text-sm ${
              adjustResult.inserted.length > 0
                ? "border-amber-300 bg-amber-50"
                : "border-line bg-surface-muted"
            }`}
          >
            <div className="flex items-start justify-between gap-2 text-ink-secondary">
              <div className="min-w-0"><ModelMarkdown text={adjustResult.reason} /></div>
              <button
                type="button"
                onClick={() => {
                  setAdjustResult(null);
                  setHighlightKeys(new Set());
                }}
                className="shrink-0 text-ink-muted hover:text-ink-secondary"
                aria-label="关闭调整结果"
              >
                ✕
              </button>
            </div>
            {adjustResult.inserted.length > 0 && (
              <ul className="mt-2 space-y-1">
                {adjustResult.inserted.map((t) => (
                  <li
                    key={t.task_id}
                    className="flex flex-wrap items-center gap-2 rounded-lg bg-white px-2.5 py-1.5 text-caption"
                  >
                    <span className="rounded bg-amber-500 px-1.5 py-0.5 font-medium text-white">
                      第 {t.day} 天
                    </span>
                    <span className="font-medium text-ink-secondary"><MathText text={t.title} /></span>
                    <span className="text-ink-muted">{t.minutes} 分钟</span>
                    <span className="text-ink-muted">
                      当前掌握度 {fmtPct(t.p_mastery)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {view?.structure.goal_analysis && (
          <details className="mb-3 rounded-xl border border-brand-100 bg-brand-50/30 px-3.5 py-3">
            <summary className="cursor-pointer text-body-sm font-medium text-brand-700">
              查看确认时的目标与验收标准
            </summary>
            <p className="mt-2 text-caption leading-5 text-ink-secondary">
              {view.structure.goal_analysis.summary}
            </p>
            <div className="mt-2 grid gap-2 text-body-sm sm:grid-cols-2">
              <div className="rounded-lg bg-white px-2.5 py-2 text-ink-secondary">
                <strong className="block text-caption text-ink-muted">可验证成果</strong>
                {view.structure.goal_analysis.outcomes?.length ? (
                  view.structure.goal_analysis.outcomes.map((item) => (
                    <p key={item} className="mt-1">• {item}</p>
                  ))
                ) : (
                  <p className="mt-1 text-ink-muted">未记录</p>
                )}
              </div>
              <div className="rounded-lg bg-white px-2.5 py-2 text-ink-secondary">
                <strong className="block text-caption text-ink-muted">验收标准</strong>
                {view.structure.goal_analysis.success_criteria?.length ? (
                  view.structure.goal_analysis.success_criteria.map((item) => (
                    <p key={item} className="mt-1">• {item}</p>
                  ))
                ) : (
                  <p className="mt-1 text-ink-muted">未记录</p>
                )}
              </div>
            </div>
            {view.structure.skill_gaps && view.structure.skill_gaps.length > 0 && (
              <p className="mt-2 text-body-sm text-warning-700">
                创建时优先补强：{view.structure.skill_gaps.map((gap) => `${gap.skill}（${gap.zone}）`).join("、")}
              </p>
            )}
          </details>
        )}

        {view?.kc_dependency?.levels.length ? (
          <KcDependencyPath
            dependency={view.kc_dependency}
            onTaskOpen={openDependencyTask}
          />
        ) : null}

        {view?.provisional_concepts?.length ? (
          <section
            aria-label="计划里的临时概念"
            className="mb-4 min-w-0 rounded-xl border border-dashed border-line-strong bg-surface-muted/40 p-3 sm:p-4"
          >
            <h3 className="text-body-sm font-semibold text-ink">计划里的临时概念</h3>
            <p className="mt-1 text-caption text-ink-muted">
              这些概念还没有资料里的知识点承接，所以不在上面的依赖图里，也不计入掌握度。
              上传或关联相关资料后，它们才会归并成正式知识点。
            </p>
            <ul className="mt-2 flex flex-wrap gap-2">
              {view.provisional_concepts.map((item) => (
                <li
                  key={item.name}
                  className="rounded-full border border-dashed border-line-strong bg-white px-3 py-1 text-caption text-ink-secondary"
                >
                  {item.name}
                </li>
              ))}
            </ul>
            {view.status === "active" && (view.anchorable_concepts ?? 0) > 0 && (
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2">
                <p className="min-w-0 text-caption leading-5 text-emerald-700">
                  资料已更新：其中 {view.anchorable_concepts} 个临时概念现在能匹配到课程知识点。确认后会建立持久对应并从临时列表移除；掌握度仍只由真实作答证据更新。
                </p>
                <button
                  type="button"
                  disabled={reAnchoring}
                  onClick={() => void reAnchor()}
                  className="shrink-0 rounded-control border border-emerald-300 bg-white px-3 py-1.5 text-caption font-medium text-emerald-800 hover:bg-emerald-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {reAnchoring ? "锚定中…" : "重新锚定"}
                </button>
              </div>
            )}
            {view.kb_id && (
              <Link
                href={`/courses/${view.kb_id}`}
                className="mt-3 inline-flex items-center gap-1 rounded-control border border-line-strong bg-white px-3 py-1.5 text-caption font-medium text-ink transition-colors hover:bg-surface-muted"
              >
                去补充或增强资料 →
              </Link>
            )}
          </section>
        ) : null}

        {detailError && (
          <div className="mb-3">
            <ErrorBanner
              message={detailError}
              onRetry={() => setDetailKey((k) => k + 1)}
            />
          </div>
        )}

        {view ? (
          <PlanCalendar
            key={view.id}
            structure={view.structure}
            currentDayNumber={view.schedule?.current_day}
            onStatus={view.status === "active" ? (taskId, next) => void updateTaskStatus(taskId, next) : undefined}
            onTaskOpen={(location) => setOpenTask({ ...location, planId: view.id, planKbId: view.kb_id, readOnly: view.status !== "active" })}
            pendingKeys={pendingKeys}
            highlightKeys={highlightKeys}
          />
        ) : detailError ? (
          <EmptyState title="该计划缺少结构数据" illustration="map" />
        ) : (
          <SkeletonList rows={4} />
        )}
      </Card>

      {taskBanner && (
        <ErrorBanner message={taskBanner} onClose={() => setTaskBanner(null)} />
      )}

      {openTask && (
        <PlanTaskDrawer
          location={openTask}
          onClose={() => setOpenTask(null)}
          onCompleted={(result) => {
            // 最后一个任务完成后计划已不可再执行；关闭旧抽屉，避免继续携带已完成计划的归因链接。
            if (result.plan_status !== "active") {
              setOpenTask(null);
              setPendingKeys(new Set());
            }
            setDetailKey((key) => key + 1);
            onChanged?.();
          }}
        />
      )}

      <Card
        title="调整历史"
        extra={
          <span className="text-caption text-ink-muted">
            共 {adjustments.length} 次
          </span>
        }
      >
        {adjustments.length === 0 ? (
          <p className="py-3 text-center text-caption text-ink-muted">
            还没有调整记录；低分测评或点击「根据学情调整计划」都会在这里留痕
          </p>
        ) : (
          <ol className="ml-1.5 space-y-4 border-l-2 border-line pl-5">
            {adjustments.map((a) => {
              const auto = a.trigger === "low_score_attempt";
              return (
                <li key={a.id} className="relative">
                  {/* 时间线节点：低分自动触发=琥珀，手动=靛蓝 */}
                  <span
                    aria-hidden
                    className={`absolute -left-[27px] top-0.5 flex h-4 w-4 items-center justify-center rounded-full ring-4 ring-white ${
                      auto ? "bg-amber-500" : "bg-indigo-500"
                    }`}
                  >
                    <svg
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="#fff"
                      strokeWidth="3"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      className="h-2.5 w-2.5"
                    >
                      {auto ? (
                        <path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" />
                      ) : (
                        <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5Z" />
                      )}
                    </svg>
                  </span>
                  <div className="rounded-xl bg-surface-muted/80 px-3 py-2.5 transition-colors hover:bg-surface-muted">
                    <p className="flex flex-wrap items-center gap-2">
                      <span className="text-caption text-ink-muted">
                        {fmtDateTime(a.at)}
                      </span>
                      <span
                        className={`rounded-full px-2 py-0.5 text-caption font-medium ${
                          auto
                            ? "bg-amber-100/70 text-amber-700"
                            : "bg-indigo-50 text-indigo-600"
                        }`}
                      >
                        {TRIGGER_LABELS[a.trigger] ?? a.trigger}
                      </span>
                      <span className="rounded-full bg-amber-50 px-2 py-0.5 text-caption font-medium text-amber-700">
                        插入 {a.inserted.length} 个任务
                      </span>
                    </p>
                    <div className="mt-1 text-sm leading-relaxed text-ink-secondary">
                      <ModelMarkdown text={a.reason} />
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </Card>
    </>
  );
}
