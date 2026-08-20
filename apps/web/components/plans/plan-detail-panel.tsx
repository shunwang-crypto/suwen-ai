"use client";

// 计划详情面板（UI 重构阶段 5 自 /plans 右列抽离的共享组件）：分段进度条 + 学情调整
// （结果横幅/补强高亮）+ 目标回看 + KC 依赖图 + 日历（含任务抽屉）+ 调整历史。
// /plans 聚合壳与 /courses/[kbId] 计划 tab 渲染同一份，交互逻辑单处维护。

import { useEffect, useState } from "react";
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
  normalizedTaskStatus,
  withTaskStatus,
  type PlanTaskStatus,
} from "@/components/plans/plan-parts";
import {
  PlanTaskDrawer,
  type PlanTaskLocation,
} from "@/components/plans/plan-task-drawer";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type {
  PlanAdjustResult,
  PlanAdjustmentItem,
  PlanDetail,
  PlanToggleResult,
} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

type DrawerLocation = PlanTaskLocation;

export function PlanDetailPanel({
  planId,
  goal,
  status,
  budgetHours,
  onChanged,
}: {
  planId: string;
  goal: string;
  status: "active" | "completed" | "archived";
  budgetHours: number;
  /** 计划列表刷新回调（任务完成/调整后可让外层同步进度摘要） */
  onChanged?: () => void;
}) {
  const [detailKey, setDetailKey] = useState(0);
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(new Set());
  const [openTask, setOpenTask] = useState<DrawerLocation | null>(null);
  const [taskBanner, setTaskBanner] = useState<string | null>(null);
  const [adjusting, setAdjusting] = useState(false);
  const [adjustResult, setAdjustResult] = useState<PlanAdjustResult | null>(null);
  const [highlightKeys, setHighlightKeys] = useState<Set<string>>(new Set());

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
    setDetail(detailRes.data);
    setDetailError(detailRes.error);
  }, [detailRes.data, detailRes.error]);

  const [adjustments, setAdjustments] = useState<PlanAdjustmentItem[]>([]);
  useEffect(() => {
    setAdjustments(adjustmentsRes.data ?? []);
  }, [adjustmentsRes.data]);
  /* eslint-enable react-hooks/set-state-in-effect */

  /** 任务三态更新：乐观更新；失败只恢复当前任务（与今日页同一模式）。 */
  const updateTaskStatus = async (taskId: string, next: PlanTaskStatus) => {
    if (!detail || detail.status !== "active" || pendingKeys.has(taskId)) return;
    const snapshot = detail;
    const previousTask = flattenDays(detail.structure)
      .flatMap((day) => day.tasks)
      .find((task) => task.task_id === taskId);
    if (!previousTask) return;
    const previousStatus = normalizedTaskStatus(previousTask);
    setDetail(withTaskStatus(detail, taskId, next));
    setPendingKeys((prev) => new Set(prev).add(taskId));
    setTaskBanner(null);
    try {
      const res = await apiJson<PlanToggleResult>(
        `/api/plans/${snapshot.id}/tasks/${taskId}/toggle`,
        { body: { status: next } },
      );
      // 以服务端进度为准（并发勾选时避免本地计数漂移）
      setDetail((prev) =>
        prev && prev.id === snapshot.id ? { ...prev, progress: res.progress } : prev,
      );
      onChanged?.();
    } catch (err) {
      setDetail((prev) =>
        prev && prev.id === snapshot.id
          ? withTaskStatus(prev, taskId, previousStatus)
          : prev,
      );
      setTaskBanner(err instanceof ApiError ? err.message : "任务状态更新失败，已回滚");
    } finally {
      setPendingKeys((prev) => {
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
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
    } catch (err) {
      setTaskBanner(err instanceof ApiError ? err.message : "计划调整失败，请重试");
    } finally {
      setAdjusting(false);
    }
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

  return (
    <>
      <Card
        title={<span className="block max-w-100 truncate">{goal}</span>}
        extra={
          <span className="flex flex-wrap items-center justify-end gap-1.5 text-caption text-slate-400">
            {view && <span className={`rounded-full px-2 py-0.5 font-medium ${view.plan_mode === "course_bound" ? "bg-success-50 text-success-700" : "bg-warning-50 text-warning-700"}`}>{view.plan_mode === "course_bound" ? "课程资料计划" : "目标起步计划"}</span>}
            <span>{status === "active" ? "进行中 · " : status === "completed" ? "已完成 · " : "已归档 · "}每日 {budgetHours} 小时</span>
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
        {/* 计划头部：任务粒度进度（分段式，每阶段一段）+ 学情调整入口 */}
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <div className="min-w-40 flex-1">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-caption font-medium text-slate-500">
                已完成 {view?.progress.done ?? 0} / {view?.progress.total ?? 0} 个任务
                {(view?.progress.skipped ?? 0) > 0 &&
                  ` · 跳过 ${view?.progress.skipped ?? 0} 个`}
              </span>
              <span className="font-mono text-caption text-slate-500">
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
                      className="h-full min-w-2 bg-slate-100"
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
              <div className="mt-1 h-2 rounded-full bg-slate-100">
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
        </div>

        {adjustResult && (
          <div
            className={`mb-3 rounded-xl border px-3.5 py-2.5 text-sm ${
              adjustResult.inserted.length > 0
                ? "border-amber-300 bg-amber-50"
                : "border-slate-200 bg-slate-50"
            }`}
          >
            <div className="flex items-start justify-between gap-2 text-slate-700">
              <div className="min-w-0"><ModelMarkdown text={adjustResult.reason} /></div>
              <button
                type="button"
                onClick={() => {
                  setAdjustResult(null);
                  setHighlightKeys(new Set());
                }}
                className="shrink-0 text-slate-400 hover:text-slate-600"
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
                    <span className="font-medium text-slate-700"><MathText text={t.title} /></span>
                    <span className="text-slate-400">{t.minutes} 分钟</span>
                    <span className="text-slate-400">
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
          onCompleted={() => setDetailKey((key) => key + 1)}
        />
      )}

      <Card
        title="调整历史"
        extra={
          <span className="text-caption text-slate-400">
            共 {adjustments.length} 次
          </span>
        }
      >
        {adjustments.length === 0 ? (
          <p className="py-3 text-center text-caption text-slate-400">
            还没有调整记录；低分测评或点击「根据学情调整计划」都会在这里留痕
          </p>
        ) : (
          <ol className="ml-1.5 space-y-4 border-l-2 border-slate-100 pl-5">
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
                  <div className="rounded-xl bg-slate-50/80 px-3 py-2.5 transition-colors hover:bg-slate-50">
                    <p className="flex flex-wrap items-center gap-2">
                      <span className="text-caption text-slate-400">
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
                    <div className="mt-1 text-sm leading-relaxed text-slate-700">
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
