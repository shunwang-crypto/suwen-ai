"use client";

// 学习计划（F3）：发起表单 → SSE 流 + interrupt 计划确认（阶段树预览）→ 确认落库；
// 计划列表 + 日历视图（stages/days/tasks 渲染为周网格卡片）。
// 「计划是活的」：任务复选框（乐观更新+失败回滚）、头部进度条、
// 「根据学情调整计划」插入补强任务并高亮、调整历史时间线。

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  SkeletonList,
  Spinner,
  fmtDateTime,
  fmtPct,
} from "@/components/common";
import { FlowTimeline, type FlowItem } from "@/components/flow-timeline";
import { PlanTaskDrawer, type PlanTaskLocation } from "@/components/plans/plan-task-drawer";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { MathText } from "@/lib/katex";
import {
  PLAN_GOAL_EXAMPLES,
  isPlanGoalReady,
  planGoalReadiness,
} from "@/lib/plan-goal";
import { streamSse } from "@/lib/sse";
import type {
  ErrorEventData,
  GraphDoneData,
  GraphInterruptData,
  GraphTraceData,
  KbOut,
  NodeUpdateData,
  PlanAdjustResult,
  PlanAdjustmentItem,
  PlanDetail,
  PlanGoalAnalysis,
  PlanListItem,
  PlanSkillGap,
  PlanStructure,
  PlanTask,
  PlanResource,
  PlanToggleResult,
} from "@/lib/types";

type Phase = "idle" | "streaming" | "await_confirm" | "resuming";
const PENDING_PLAN_KEY = "suwen:pending-plan-draft";

interface PendingPlanDraft {
  status: "generating" | "await_confirm" | "resuming";
  interrupt?: GraphInterruptData;
  startedAt: number;
  form: {
    goal: string;
    currentLevel: string;
    targetOutcome: string;
    successCriteria: string;
    kbId: string;
    hours: number;
    totalDays: number;
  };
}

function pendingPlanKey(userId: string): string {
  return `${PENDING_PLAN_KEY}:${userId}`;
}

function readPendingPlan(userId: string): PendingPlanDraft | null {
  try {
    const key = pendingPlanKey(userId);
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PendingPlanDraft>;
    const validStatus = ["generating", "await_confirm", "resuming"].includes(
      value.status ?? "",
    );
    const stale = !value.startedAt || Date.now() - value.startedAt > 30 * 60 * 1000;
    const invalidInterrupt =
      value.status !== "generating" &&
      (!value.interrupt?.thread_id?.startsWith("plan:") ||
        !value.interrupt.resume_url ||
        !value.interrupt.plan);
    if (!validStatus || !value.form?.goal || stale || invalidInterrupt) {
      window.localStorage.removeItem(key);
      return null;
    }
    return value as PendingPlanDraft;
  } catch {
    window.localStorage.removeItem(pendingPlanKey(userId));
    return null;
  }
}

function clearPendingPlan(userId: string): void {
  window.localStorage.removeItem(pendingPlanKey(userId));
}

function writePendingPlan(userId: string, value: PendingPlanDraft): void {
  window.localStorage.setItem(pendingPlanKey(userId), JSON.stringify(value));
}

const emptySubscribe = () => () => {};

const TRIGGER_LABELS: Record<string, string> = {
  manual: "手动调整",
  low_score_attempt: "低分测评触发",
};

const MISSING_GOAL_LABELS: Record<string, string> = {
  scope: "学习范围",
  current_level: "当前基础",
  target_outcome: "期望成果",
  success_criteria: "验收标准",
};

/** 阶段配色（固定顺序分配，不随过滤变化）。 */
const STAGE_COLORS = [
  { chip: "bg-success-50 text-success-700", bar: "bg-success-500" },
  { chip: "bg-brand-50 text-brand-700", bar: "bg-brand-600" },
  { chip: "bg-surface-muted text-ink-muted", bar: "bg-line-strong" },
  { chip: "bg-surface-muted text-ink-muted", bar: "bg-line-strong" },
  { chip: "bg-surface-muted text-ink-muted", bar: "bg-line-strong" },
];

interface FlatDay {
  day: number;
  stageIdx: number;
  stageName: string;
  tasks: PlanStructure["stages"][number]["days"][number]["tasks"];
}

function flattenDays(structure: PlanStructure | null): FlatDay[] {
  if (!structure?.stages) return [];
  const out: FlatDay[] = [];
  structure.stages.forEach((stage, stageIdx) => {
    for (const day of stage.days ?? []) {
      out.push({
        day: day.day,
        stageIdx,
        stageName: stage.name,
        tasks: day.tasks ?? [],
      });
    }
  });
  return out.sort((a, b) => a.day - b.day);
}

type PlanTaskStatus = NonNullable<PlanTask["status"]>;

function normalizedTaskStatus(task: PlanTask): PlanTaskStatus {
  return task.status ?? (task.done ? "done" : "pending");
}

function taskIsPending(task: PlanTask): boolean {
  return normalizedTaskStatus(task) === "pending";
}

/** 乐观更新：生成带新三态的 detail 副本，并完整重算两种进度口径。 */
function withTaskStatus(
  detail: PlanDetail,
  taskKey: string,
  status: PlanTaskStatus,
): PlanDetail {
  const stages = detail.structure.stages.map((stage) => ({
    ...stage,
    days: (stage.days ?? []).map((day) => ({
      ...day,
      tasks: (day.tasks ?? []).map((task) => {
        if (task.task_key !== taskKey) return task;
        return { ...task, status, done: status === "done" };
      }),
    })),
  }));
  const tasks = stages.flatMap((stage) =>
    (stage.days ?? []).flatMap((day) => day.tasks ?? []),
  );
  const total = tasks.length;
  const done = tasks.filter((task) => normalizedTaskStatus(task) === "done").length;
  const skipped = tasks.filter((task) => normalizedTaskStatus(task) === "skipped").length;
  const pending = total - done - skipped;
  const effectiveTotal = total - skipped;
  return {
    ...detail,
    structure: { ...detail.structure, stages },
    progress: {
      total,
      done,
      skipped,
      pending,
      ratio: total ? done / total : 0,
      effective_ratio: effectiveTotal ? done / effectiveTotal : total ? 1 : 0,
    },
  };
}

function TaskLockBadge({ task }: { task: PlanTask }) {
  const blockers = task.lock_blockers ?? [];
  if (!task.locked || blockers.length === 0) return null;
  // 服务端只下发定性区间（掌握度小数不出境），这里原样展示。
  const detail = blockers.map((b) => `${b.kc_name}（${b.zone}）`).join("、");
  return (
    <span
      title={`需先掌握：${detail}`}
      className="inline-flex items-center gap-0.5 rounded bg-surface-muted px-1 py-0.5 font-medium text-ink-secondary"
    >
      🔒 需先掌握 {blockers[0].kc_name}
      {blockers.length > 1 ? ` 等 ${blockers.length} 项` : ""}
    </span>
  );
}

function TaskStatusControls({
  task,
  taskKey,
  busy,
  disabled,
  compact = false,
  onStatus,
}: {
  task: PlanTask;
  taskKey?: string;
  busy: boolean;
  disabled: boolean;
  compact?: boolean;
  onStatus?: (taskKey: string, status: PlanTaskStatus) => void;
}) {
  const status = normalizedTaskStatus(task);
  const unavailable = disabled || !taskKey || busy;
  return (
    <div className="flex shrink-0 items-center gap-1">
      <input
        type="checkbox"
        checked={status === "done"}
        disabled={unavailable}
        onChange={() =>
          taskKey && onStatus?.(taskKey, status === "done" ? "pending" : "done")
        }
        aria-label={`${status === "done" ? "取消完成" : "标记完成"}：${task.title}`}
        className={`${compact ? "h-3.5 w-3.5" : "h-4 w-4"} accent-brand-600`}
      />
      <button
        type="button"
        disabled={unavailable}
        aria-pressed={status === "skipped"}
        aria-label={`${status === "skipped" ? "取消跳过" : "跳过任务"}：${task.title}`}
        onClick={() =>
          taskKey && onStatus?.(taskKey, status === "skipped" ? "pending" : "skipped")
        }
        className={`rounded px-1.5 py-0.5 text-[9px] font-medium disabled:cursor-not-allowed disabled:opacity-40 ${
          status === "skipped"
            ? "bg-surface-muted text-ink-secondary"
            : "text-ink-muted hover:bg-surface-muted hover:text-ink-secondary"
        }`}
      >
        {status === "skipped" ? "已跳过" : "跳过"}
      </button>
    </div>
  );
}

/** 日历视图：按 7 天一周分组的网格卡片；支持完成、跳过和恢复待办。 */
function PlanCalendar({
  structure,
  onStatus,
  onTaskOpen,
  pendingKeys,
  highlightKeys,
}: {
  structure: PlanStructure;
  onStatus?: (taskKey: string, status: PlanTaskStatus) => void;
  onTaskOpen?: (location: Omit<PlanTaskLocation, "planKbId">) => void;
  pendingKeys?: Set<string>;
  highlightKeys?: Set<string>;
}) {
  const [showFull, setShowFull] = useState(false);
  const days = flattenDays(structure);
  if (days.length === 0) {
    return <EmptyState title="该计划暂无日程" illustration="map" />;
  }
  const firstPendingIndex = days.findIndex((day) => day.tasks.some(taskIsPending));
  const currentIndex = firstPendingIndex >= 0 ? firstPendingIndex : days.length - 1;
  const currentDay = days[currentIndex] ?? days[0];
  const upcoming = days.slice(currentIndex, currentIndex + 7);
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] lg:items-start">
        <section className="overflow-hidden rounded-[12px] border border-line bg-white">
        <header className="flex items-center justify-between border-b border-line px-4 py-3"><h3 className="text-[13px] font-semibold text-ink">今日任务 <span className="ml-1 font-normal text-ink-muted">第 {currentDay.day} 天</span></h3><span className="text-[10px] text-ink-muted">{currentDay.tasks.filter((task) => normalizedTaskStatus(task) === "done").length} 完成 · {currentDay.tasks.filter((task) => normalizedTaskStatus(task) === "skipped").length} 跳过</span></header>
        <ul className="divide-y divide-line">
          {currentDay.tasks.map((task, index) => {
            const key = task.task_key;
            const status = normalizedTaskStatus(task);
            const busy = Boolean(key && pendingKeys?.has(key));
            const isNew = Boolean(key && highlightKeys?.has(key));
            return <li key={key ?? index} className={`flex items-center gap-3 px-4 py-3 ${isNew ? "bg-warning-50" : ""}`}><TaskStatusControls task={task} taskKey={key} busy={busy} disabled={!onStatus} onStatus={onStatus} /><div className="min-w-0 flex-1"><button type="button" onClick={() => onTaskOpen?.({ task, stageName: currentDay.stageName, day: currentDay.day })} className={`block max-w-full truncate text-left text-[12px] hover:text-brand-700 ${status === "done" ? "text-ink-muted line-through" : status === "skipped" || task.locked ? "text-ink-muted" : "font-medium text-ink"}`}><MathText text={task.title} singleLine /></button><p className="mt-1 flex gap-2 text-[10px] text-ink-muted"><span>{task.kc_names[0] || currentDay.stageName}</span>{isNew && <span className="font-medium text-warning-600">补强</span>}<TaskLockBadge task={task} /></p></div><span className="text-[10px] text-ink-muted">{task.minutes} 分钟</span></li>;
          })}
        </ul>
        <p className="border-t border-line px-4 py-2.5 text-[10px] text-ink-muted">今日预计 {currentDay.tasks.reduce((sum, task) => sum + (task.minutes || 0), 0)} 分钟</p>
        </section>

        <section className="overflow-hidden rounded-[12px] border border-line bg-white">
        <header className="flex items-center justify-between border-b border-line px-4 py-3"><h3 className="text-[13px] font-semibold text-ink">近期日程 <span className="ml-1 font-normal text-ink-muted">第 {upcoming[0]?.day ?? 1}–{upcoming.at(-1)?.day ?? 1} 天</span></h3><button type="button" aria-expanded={showFull} onClick={() => setShowFull((value) => !value)} className="text-[10px] font-medium text-brand-700 hover:text-brand-800">{showFull ? "收起完整计划" : `查看完整计划（${days.length} 天）`}</button></header>
        <ol className="divide-y divide-line">{upcoming.map((day) => { const done = day.tasks.filter((task) => normalizedTaskStatus(task) === "done").length; const skipped = day.tasks.filter((task) => normalizedTaskStatus(task) === "skipped").length; const handled = done + skipped; const totalMinutes = day.tasks.reduce((sum, task) => sum + (task.minutes || 0), 0); return <li key={day.day} className={`grid grid-cols-[32px_minmax(0,1fr)_auto] items-center gap-3 px-4 py-3 ${day.day === currentDay.day ? "bg-brand-50/50" : ""}`}><strong className={`text-[13px] ${day.day === currentDay.day ? "text-brand-700" : "text-ink-secondary"}`}>{day.day}<span className="block text-[9px] font-normal text-ink-muted">天</span></strong><div className="min-w-0"><p className="truncate text-[12px] font-medium text-ink">{day.stageName}</p><p className="mt-0.5 text-[10px] text-ink-muted">{day.tasks.length} 个任务 · {totalMinutes} 分钟{skipped > 0 ? ` · ${skipped} 个跳过` : ""}</p></div><span className={`text-[10px] ${handled === day.tasks.length ? "text-success-600" : day.day === currentDay.day ? "text-brand-700" : "text-ink-muted"}`}>{handled === day.tasks.length ? `✓ ${done} 完成` : day.day === currentDay.day ? `${done}/${day.tasks.length - skipped} 进行中` : "待开始"}</span></li>; })}</ol>
        </section>
      </div>

      {showFull && (
        <section className="rounded-[12px] border border-line bg-surface-muted/40 p-4" aria-label="完整学习计划">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-[13px] font-semibold text-ink">完整计划</h3>
              <p className="mt-0.5 text-[10px] text-ink-muted">共 {structure.stages.length} 个阶段、{days.length} 天；可直接勾选任意一天的任务</p>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {structure.stages.map((stage, index) => {
                const color = STAGE_COLORS[index % STAGE_COLORS.length];
                return <span key={`${stage.name}-${index}`} className={`rounded-full px-2.5 py-1 text-[10px] font-medium ${color.chip}`}><span className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full ${color.bar}`} />{stage.name} · {stage.days?.length ?? 0} 天</span>;
              })}
            </div>
          </div>
          <div className="space-y-5">
            {structure.stages.map((stage, stageIndex) => {
              const color = STAGE_COLORS[stageIndex % STAGE_COLORS.length];
              return (
                <div key={`${stage.name}-${stageIndex}`}>
                  <h4 className="mb-2 flex items-center gap-2 text-[12px] font-semibold text-ink"><span className={`h-2 w-2 rounded-full ${color.bar}`} />{stage.name}</h4>
                  <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                    {(stage.days ?? []).map((day) => {
                      const totalMinutes = day.tasks.reduce((sum, task) => sum + (task.minutes || 0), 0);
                      return (
                        <article key={day.day} className={`overflow-hidden rounded-[10px] border bg-white ${day.day === currentDay.day ? "border-brand-300 ring-1 ring-brand-100" : "border-line"}`}>
                          <header className="flex items-center justify-between border-b border-line px-3 py-2"><strong className="text-[11px] text-ink">第 {day.day} 天</strong><span className="text-[9px] text-ink-muted">{totalMinutes} 分钟</span></header>
                          <ul className="divide-y divide-line/70">
                            {day.tasks.map((task, taskIndex) => {
                              const key = task.task_key;
                              const status = normalizedTaskStatus(task);
                              const busy = Boolean(key && pendingKeys?.has(key));
                              const isNew = Boolean(key && highlightKeys?.has(key));
                              return (
                                <li key={key ?? taskIndex} className={`flex items-start gap-2 px-3 py-2.5 ${isNew ? "bg-warning-50" : ""}`}>
                                  <TaskStatusControls task={task} taskKey={key} busy={busy} disabled={!onStatus} compact onStatus={onStatus} />
                                  <div className="min-w-0 flex-1"><button type="button" onClick={() => onTaskOpen?.({ task, stageName: stage.name, day: day.day })} className={`block text-left text-[11px] leading-relaxed hover:text-brand-700 ${status === "done" ? "text-ink-muted line-through" : status === "skipped" || task.locked ? "text-ink-muted" : "text-ink"}`}><MathText text={task.title} /></button><p className="mt-1 flex flex-wrap gap-1 text-[9px] text-ink-muted"><span>{task.minutes} 分钟</span>{task.kc_names.map((name) => <span key={name} className={`rounded px-1 py-0.5 ${color.chip}`}>{name}</span>)}{status === "skipped" && <span>已跳过</span>}{isNew && <span className="font-medium text-warning-600">补强任务</span>}<TaskLockBadge task={task} /></p></div>
                                </li>
                              );
                            })}
                          </ul>
                        </article>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}

/** 草案编辑器（F3「生成后供用户编辑确认」）：本地副本编辑，确认时整份回传。
 *
 * 作用域刻意收窄——只做标题 / 分钟数 / 删除 / 同天上下移，不做拖拽与跨天移动：
 * 服务端 confirm_plan 会对回传计划重跑硬校验链（总天数 / 日预算 / 资源引用 /
 * 先修序），保存后以服务端返回为准，故前端只需拦住明显非法输入。
 */
function PlanConfirmCard({
  plan,
  resources,
  goalAnalysis,
  skillGaps,
  onConfirm,
  onCancel,
}: {
  plan: PlanStructure;
  resources: PlanResource[];
  goalAnalysis?: PlanGoalAnalysis;
  skillGaps?: PlanSkillGap[];
  onConfirm: (edited: PlanStructure) => void;
  onCancel: () => void;
}) {
  // 本地草案副本：编辑不影响 interrupt 原始负载（取消后重新打开仍是原稿）
  const [draft, setDraft] = useState<PlanStructure>(() =>
    JSON.parse(JSON.stringify(plan)) as PlanStructure,
  );
  const [touched, setTouched] = useState(false);

  const totalDays = draft.stages.reduce((s, st) => s + (st.days?.length ?? 0), 0);
  const totalTasks = draft.stages.reduce(
    (s, st) => s + (st.days ?? []).reduce((x, d) => x + d.tasks.length, 0),
    0,
  );

  /** 就地校验：标题非空 + 分钟为正整数；有错则禁止确认。 */
  const invalid = draft.stages.some((st) =>
    (st.days ?? []).some((d) =>
      d.tasks.some(
        (t) =>
          !t.title.trim() ||
          !Number.isInteger(t.minutes) ||
          t.minutes <= 0 ||
          t.minutes > 1440,
      ),
    ),
  );
  const emptied = totalTasks === 0;

  /** 以不可变方式改写某一天的任务列表。 */
  const mutateDay = (
    si: number,
    di: number,
    fn: (tasks: PlanTask[]) => PlanTask[],
  ) => {
    setTouched(true);
    setDraft((prev) => {
      const next = JSON.parse(JSON.stringify(prev)) as PlanStructure;
      const day = next.stages[si]?.days?.[di];
      if (day) day.tasks = fn(day.tasks);
      return next;
    });
  };

  const editTask = (si: number, di: number, ti: number, patch: Partial<PlanTask>) =>
    mutateDay(si, di, (tasks) =>
      tasks.map((t, i) => (i === ti ? { ...t, ...patch } : t)),
    );

  const removeTask = (si: number, di: number, ti: number) =>
    mutateDay(si, di, (tasks) => tasks.filter((_, i) => i !== ti));

  /** 同一天内上下移动（跨天移动交给服务端预算校验，前端不做）。 */
  const moveTask = (si: number, di: number, ti: number, dir: -1 | 1) =>
    mutateDay(si, di, (tasks) => {
      const to = ti + dir;
      if (to < 0 || to >= tasks.length) return tasks;
      const next = [...tasks];
      [next[ti], next[to]] = [next[to], next[ti]];
      return next;
    });

  const iconBtn =
    "rounded-lg border border-slate-200 bg-white px-1.5 py-0.5 text-[11px] text-slate-500 transition-colors hover:border-indigo-300 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-30";

  return (
    <div className="rounded-[14px] border border-line bg-white p-5">
      <p className="text-[10px] font-medium text-brand-700">草案确认</p>
      <h3 className="mt-2 text-[22px] font-semibold tracking-[-0.03em] text-ink">{totalDays} 天学习计划</h3>
      <p className="mt-0.5 text-xs text-slate-500">
        共 {draft.stages.length} 个阶段 / {totalDays} 天 / {totalTasks} 个任务
        {resources.length > 0 && ` · 关联 ${resources.length} 条学习资源`}
        ，可直接修改任务标题与时长，确认后才会保存。
      </p>

      {goalAnalysis && (
        <section className="mt-4 rounded-xl border border-brand-100 bg-brand-50/40 p-3" aria-label="目标拆解">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-xs font-semibold text-ink">目标拆解</h4>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                goalAnalysis.quality === "clear"
                  ? "bg-success-50 text-success-700"
                  : "bg-warning-50 text-warning-700"
              }`}
            >
              {goalAnalysis.quality === "clear" ? "信息完整" : "建议后续补充"}
            </span>
          </div>
          <p className="mt-2 text-xs leading-5 text-ink-secondary">
            {goalAnalysis.summary}
          </p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <div className="rounded-lg bg-white px-2.5 py-2">
              <p className="text-[10px] font-medium text-ink-muted">可验证成果</p>
              {goalAnalysis.outcomes.length > 0 ? (
                <ul className="mt-1 space-y-1 text-[11px] leading-4 text-ink-secondary">
                  {goalAnalysis.outcomes.map((item) => <li key={item}>• {item}</li>)}
                </ul>
              ) : <p className="mt-1 text-[11px] text-warning-700">尚未明确</p>}
            </div>
            <div className="rounded-lg bg-white px-2.5 py-2">
              <p className="text-[10px] font-medium text-ink-muted">验收标准</p>
              {goalAnalysis.success_criteria.length > 0 ? (
                <ul className="mt-1 space-y-1 text-[11px] leading-4 text-ink-secondary">
                  {goalAnalysis.success_criteria.map((item) => <li key={item}>• {item}</li>)}
                </ul>
              ) : <p className="mt-1 text-[11px] text-warning-700">未提供，系统不会自行编造</p>}
            </div>
          </div>
          {goalAnalysis.missing_information.length > 0 && (
            <p className="mt-2 text-[11px] text-warning-700">
              待明确：{goalAnalysis.missing_information.map((item) => MISSING_GOAL_LABELS[item] ?? item).join("、")}。计划已在前段安排基线任务，也可返回补充后重新生成。
            </p>
          )}
          {skillGaps && skillGaps.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5" aria-label="优先补强技能">
              {skillGaps.map((gap) => (
                <span key={`${gap.skill}-${gap.zone}`} className="rounded-full border border-warning-200 bg-white px-2 py-1 text-[10px] text-warning-700">
                  优先补强：{gap.skill}（{gap.zone}）
                </span>
              ))}
            </div>
          )}
        </section>
      )}

      <ul className="mt-3 space-y-2">
        {draft.stages.map((stage, si) => {
          const color = STAGE_COLORS[si % STAGE_COLORS.length];
          const minutes = (stage.days ?? []).reduce(
            (s, d) => s + d.tasks.reduce((x, t) => x + (t.minutes || 0), 0),
            0,
          );
          return (
            <li key={si} className="rounded-xl border border-indigo-100 bg-white p-3">
              <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-slate-800">
                <span className={`h-2 w-2 rounded-full ${color.bar}`} />
                {stage.name}
                <span className="text-xs font-normal text-slate-400">
                  {stage.days?.length ?? 0} 天 · 约 {Math.round(minutes / 60)} 小时
                </span>
              </p>
              <ul className="mt-2 space-y-2">
                {(stage.days ?? []).map((d, di) => (
                  <li key={d.day} className="rounded-lg bg-slate-50/70 p-2">
                    <p className="mb-1 text-[11px] font-medium text-slate-500">
                      第 {d.day} 天
                    </p>
                    {d.tasks.length === 0 ? (
                      <p className="px-1 text-[11px] text-amber-600">
                        本日已无任务，保存时服务端会按预算补一条自主复习
                      </p>
                    ) : (
                      <ul className="space-y-1">
                        {d.tasks.map((t, ti) => {
                          const titleBad = !t.title.trim();
                          const minutesBad =
                            !Number.isInteger(t.minutes) ||
                            t.minutes <= 0 ||
                            t.minutes > 1440;
                          return (
                            <li key={ti} className="flex flex-wrap items-center gap-1.5">
                              <input
                                value={t.title}
                                onChange={(e) =>
                                  editTask(si, di, ti, { title: e.target.value })
                                }
                                aria-label={`第 ${d.day} 天任务 ${ti + 1} 标题`}
                                aria-invalid={titleBad}
                                className={`min-w-0 flex-1 rounded-lg border px-2 py-1 text-xs outline-none transition-colors focus:ring-2 focus:ring-indigo-100 ${
                                  titleBad
                                    ? "border-rose-300 bg-rose-50"
                                    : "border-slate-200 focus:border-indigo-300"
                                }`}
                              />
                              <input
                                type="number"
                                min={1}
                                max={1440}
                                step={5}
                                value={t.minutes}
                                onChange={(e) =>
                                  editTask(si, di, ti, {
                                    minutes: Number(e.target.value),
                                  })
                                }
                                aria-label={`第 ${d.day} 天任务 ${ti + 1} 分钟数`}
                                aria-invalid={minutesBad}
                                className={`w-16 shrink-0 rounded-lg border px-1.5 py-1 text-right text-xs outline-none transition-colors focus:ring-2 focus:ring-indigo-100 ${
                                  minutesBad
                                    ? "border-rose-300 bg-rose-50"
                                    : "border-slate-200 focus:border-indigo-300"
                                }`}
                              />
                              <span className="shrink-0 text-[11px] text-slate-400">分钟</span>
                              <span className="flex shrink-0 items-center gap-1">
                                <button
                                  type="button"
                                  onClick={() => moveTask(si, di, ti, -1)}
                                  disabled={ti === 0}
                                  title="在本日内上移"
                                  aria-label="上移"
                                  className={iconBtn}
                                >
                                  ↑
                                </button>
                                <button
                                  type="button"
                                  onClick={() => moveTask(si, di, ti, 1)}
                                  disabled={ti === d.tasks.length - 1}
                                  title="在本日内下移"
                                  aria-label="下移"
                                  className={iconBtn}
                                >
                                  ↓
                                </button>
                                <button
                                  type="button"
                                  onClick={() => removeTask(si, di, ti)}
                                  title="删除该任务"
                                  aria-label="删除"
                                  className="rounded-lg border border-slate-200 bg-white px-1.5 py-0.5 text-[11px] text-slate-400 transition-colors hover:border-rose-300 hover:text-rose-600"
                                >
                                  删除
                                </button>
                              </span>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            </li>
          );
        })}
      </ul>

      {resources.length > 0 && (
        <details className="mt-3 rounded-xl border border-indigo-100 bg-white p-3">
          <summary className="cursor-pointer text-xs font-medium text-slate-600">
            学习资源（{resources.length}）
          </summary>
          <ul className="mt-1.5 space-y-0.5">
            {resources.map((r, i) => (
              <li key={i} className="truncate text-xs text-slate-500">
                <span className="mr-1 rounded bg-slate-100 px-1 py-0.5 text-[10px] text-slate-500">
                  {r.type === "kb" ? "知识库" : "网页"}
                </span>
                <MathText text={r.title} />
              </li>
            ))}
          </ul>
        </details>
      )}

      {(invalid || emptied) && (
        <p className="mt-2 rounded-xl bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {emptied
            ? "计划不能全部删空，至少保留一个任务。"
            : "请修正标红项：任务标题不能为空，时长需为 1–1440 之间的整数分钟。"}
        </p>
      )}
      {touched && !invalid && !emptied && (
        <p className="mt-2 text-[11px] text-slate-400">
          已修改草案；保存时服务端会重新校验总时长与先修顺序，并以校验后的版本为准。
        </p>
      )}

      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-[8px] border border-line-strong bg-white px-4 py-2 text-[12px] font-medium text-ink-secondary hover:bg-surface-muted"
        >
          返回修改目标
        </button>
        <button
          type="button"
          disabled={invalid || emptied}
          onClick={() => onConfirm(draft)}
          className="rounded-[8px] bg-brand-600 px-4 py-2 text-[12px] font-medium text-white transition-colors hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {touched ? "确认并保存修改后的计划" : "确认并保存计划"}
        </button>
      </div>
    </div>
  );
}

export default function PlansPage() {
  const { user } = useAuth();
  const userId = user?.id ?? "anonymous";
  const search = useSyncExternalStore(
    emptySubscribe,
    () => window.location.search,
    () => "",
  );
  const requestedPlanId = new URLSearchParams(search).get("id");
  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [plans, setPlans] = useState<PlanListItem[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PlanListItem | null>(null);

  // 计划详情（含 done/进度）+ 调整历史
  const [detail, setDetail] = useState<PlanDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [adjustments, setAdjustments] = useState<PlanAdjustmentItem[]>([]);
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(new Set());
  const [highlightKeys, setHighlightKeys] = useState<Set<string>>(new Set());
  const [adjusting, setAdjusting] = useState(false);
  const [adjustResult, setAdjustResult] = useState<PlanAdjustResult | null>(null);
  const [taskBanner, setTaskBanner] = useState<string | null>(null);
  const [detailKey, setDetailKey] = useState(0);
  const [openTask, setOpenTask] = useState<PlanTaskLocation | null>(null);

  // 表单
  const [goal, setGoal] = useState("");
  const [currentLevel, setCurrentLevel] = useState("");
  const [targetOutcome, setTargetOutcome] = useState("");
  const [successCriteria, setSuccessCriteria] = useState("");
  const [kbId, setKbId] = useState("");
  const [hours, setHours] = useState(1.5);
  const [totalDays, setTotalDays] = useState(14);
  const [showCreate, setShowCreate] = useState(false);

  // SSE 流
  const [phase, setPhase] = useState<Phase>("idle");
  const [timeline, setTimeline] = useState<FlowItem[]>([]);
  const [interruptData, setInterruptData] = useState<GraphInterruptData | null>(null);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // 首个 setState 在 await 之后（effect 内不允许同步 setState）
  const loadPlans = useCallback(async (): Promise<PlanListItem[]> => {
    try {
      const list = await apiJson<PlanListItem[]>("/api/plans");
      setPlans(list);
      setListError(null);
      return list;
    } catch (err) {
      setPlans((prev) => prev ?? []);
      setListError(err instanceof ApiError ? err.message : "计划列表加载失败");
      return [];
    }
  }, []);

  // 首屏并行拉取计划与知识库列表（effect 内联 IIFE，同 chat 页模式）
  useEffect(() => {
    let alive = true;
    (async () => {
      const [plansRes, kbsRes] = await Promise.allSettled([
        apiJson<PlanListItem[]>("/api/plans"),
        apiJson<KbOut[]>("/api/kb"),
      ]);
      if (!alive) return;
      if (plansRes.status === "fulfilled") {
        setPlans(plansRes.value);
        setSelected(
          plansRes.value.find((plan) => plan.id === requestedPlanId) ??
            plansRes.value[0] ??
            null,
        );
      }
      else {
        setPlans([]);
        setListError("计划列表加载失败");
      }
      setKbs(kbsRes.status === "fulfilled" ? kbsRes.value : []);
      const pending = readPendingPlan(userId);
      if (pending) {
        setGoal(pending.form.goal);
        setCurrentLevel(pending.form.currentLevel ?? "");
        setTargetOutcome(pending.form.targetOutcome ?? "");
        setSuccessCriteria(pending.form.successCriteria ?? "");
        setKbId(pending.form.kbId);
        setHours(pending.form.hours);
        setTotalDays(pending.form.totalDays);
        setSelected(null);
        setShowCreate(true);
        setInterruptData(pending.interrupt ?? null);
        setPhase(
          pending.status === "generating"
            ? "streaming"
            : pending.status === "resuming"
              ? "resuming"
              : "await_confirm",
        );
      }
    })();
    return () => {
      alive = false;
    };
  }, [requestedPlanId, userId]);

  useEffect(() => {
    if (phase !== "streaming" && phase !== "resuming") return;
    const timer = setInterval(() => {
      const pending = readPendingPlan(userId);
      if (!pending) {
        setPhase("idle");
        void loadPlans();
        return;
      }
      if (pending.status === "await_confirm" && pending.interrupt) {
        setInterruptData(pending.interrupt);
        setPhase("await_confirm");
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [phase, loadPlans, userId]);

  // 选中计划后拉详情 + 调整历史（detailKey 自增可强制重拉；首个 setState 在 await 之后）
  const selectedPlanId = selected?.id ?? "";
  useEffect(() => {
    if (!selectedPlanId) return;
    let alive = true;
    (async () => {
      const [detailRes, adjRes] = await Promise.allSettled([
        apiJson<PlanDetail>(`/api/plans/${selectedPlanId}`),
        apiJson<PlanAdjustmentItem[]>(`/api/plans/${selectedPlanId}/adjustments`),
      ]);
      if (!alive) return;
      if (detailRes.status === "fulfilled") {
        setDetail(detailRes.value);
        setDetailError(null);
      } else {
        setDetail(null);
        setDetailError(
          detailRes.reason instanceof ApiError
            ? detailRes.reason.message
            : "计划详情加载失败",
        );
      }
      setAdjustments(adjRes.status === "fulfilled" ? adjRes.value : []);
    })();
    return () => {
      alive = false;
    };
  }, [selectedPlanId, detailKey]);

  /** 切换选中计划：详情态在事件处理器里清空（effect 内不允许同步 setState）。 */
  const selectPlan = (plan: PlanListItem | null) => {
    setSelected(plan);
    setDetail(null);
    setDetailError(null);
    setAdjustments([]);
    setAdjustResult(null);
    setHighlightKeys(new Set());
    setTaskBanner(null);
    setOpenTask(null);
  };

  /** 任务三态更新：乐观更新；失败只恢复当前任务，保留其他并发更新。 */
  const updateTaskStatus = async (taskKey: string, status: PlanTaskStatus) => {
    if (!detail || pendingKeys.has(taskKey)) return;
    const snapshot = detail;
    const previousTask = flattenDays(detail.structure)
      .flatMap((day) => day.tasks)
      .find((task) => task.task_key === taskKey);
    if (!previousTask) return;
    const previousStatus = normalizedTaskStatus(previousTask);
    setDetail(withTaskStatus(detail, taskKey, status));
    setPendingKeys((prev) => new Set(prev).add(taskKey));
    setTaskBanner(null);
    try {
      const res = await apiJson<PlanToggleResult>(
        `/api/plans/${snapshot.id}/tasks/${taskKey}/toggle`,
        { body: { status } },
      );
      // 以服务端进度为准（并发勾选时避免本地计数漂移）
      setDetail((prev) =>
        prev && prev.id === snapshot.id ? { ...prev, progress: res.progress } : prev,
      );
    } catch (err) {
      setDetail((prev) =>
        prev && prev.id === snapshot.id
          ? withTaskStatus(prev, taskKey, previousStatus)
          : prev,
      );
      setTaskBanner(
        err instanceof ApiError ? err.message : "任务状态更新失败，已回滚",
      );
    } finally {
      setPendingKeys((prev) => {
        const next = new Set(prev);
        next.delete(taskKey);
        return next;
      });
    }
  };

  /** 根据学情调整计划：插入补强任务 → 重拉详情并高亮新任务。 */
  const runAdjust = async () => {
    if (!detail || adjusting) return;
    setAdjusting(true);
    setTaskBanner(null);
    try {
      const res = await apiJson<PlanAdjustResult>(`/api/plans/${detail.id}/adjust`, {
        method: "POST",
        body: {},
      });
      setAdjustResult(res);
      setHighlightKeys(new Set(res.inserted.map((t) => t.task_key)));
      setDetailKey((k) => k + 1);
    } catch (err) {
      setTaskBanner(err instanceof ApiError ? err.message : "计划调整失败，请重试");
    } finally {
      setAdjusting(false);
    }
  };

  const consume = useCallback(
    async (path: string, body: unknown) => {
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        for await (const msg of streamSse(path, body, {
          signal: controller.signal,
        })) {
          switch (msg.event) {
            case "node_update":
              setTimeline((prev) => [
                ...prev,
                { kind: "node", data: msg.data as NodeUpdateData },
              ]);
              break;
            case "trace_event":
              setTimeline((prev) => [
                ...prev,
                { kind: "trace", data: msg.data as GraphTraceData },
              ]);
              break;
            case "interrupt":
              {
                const interrupt = msg.data as GraphInterruptData;
                const pending = readPendingPlan(userId);
                writePendingPlan(userId, {
                  status: "await_confirm",
                  interrupt,
                  startedAt: pending?.startedAt ?? Date.now(),
                  form: {
                    goal,
                    currentLevel,
                    targetOutcome,
                    successCriteria,
                    kbId,
                    hours,
                    totalDays,
                  },
                });
                setInterruptData(interrupt);
              }
              setPhase("await_confirm");
              break;
            case "done": {
              const d = msg.data as GraphDoneData;
              const planId = d.result?.plan_id;
              clearPendingPlan(userId);
              setPhase("idle");
              setInterruptData(null);
              if (planId) {
                setNotice(null);
                setDetail(null);
                setAdjustments([]);
                setAdjustResult(null);
                setHighlightKeys(new Set());
                void loadPlans().then((list) => {
                  setSelected(
                    list.find((p) => p.id === planId) ?? list[0] ?? null,
                  );
                });
              } else {
                setNotice("已取消，计划未保存。");
              }
              break;
            }
            case "error": {
              const d = msg.data as ErrorEventData;
              setFlowError(d.message || d.code);
              const pending = readPendingPlan(userId);
              if (path.includes("/api/threads/") && pending?.interrupt) {
                writePendingPlan(userId, { ...pending, status: "await_confirm" });
                setPhase("await_confirm");
              } else {
                clearPendingPlan(userId);
                setPhase("idle");
              }
              break;
            }
            default:
              break;
          }
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;
        setFlowError(
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : "网络异常，请稍后重试",
        );
        const pending = readPendingPlan(userId);
        if (path.includes("/api/threads/") && pending?.interrupt) {
          writePendingPlan(userId, { ...pending, status: "await_confirm" });
          setPhase("await_confirm");
        } else {
          clearPendingPlan(userId);
          setPhase("idle");
        }
      } finally {
        abortRef.current = null;
        // 流意外结束（无 done/interrupt）时兜底回到空闲态
        setPhase((p) => (p === "await_confirm" ? p : "idle"));
      }
    },
    [
      currentLevel,
      goal,
      hours,
      kbId,
      loadPlans,
      successCriteria,
      targetOutcome,
      totalDays,
      userId,
    ],
  );

  const startPlan = (e: FormEvent) => {
    e.preventDefault();
    const goalForm = { goal, currentLevel, targetOutcome, successCriteria };
    if (!isPlanGoalReady(goalForm) || phase !== "idle") return;
    clearPendingPlan(userId);
    writePendingPlan(userId, {
      status: "generating",
      startedAt: Date.now(),
      form: {
        goal: goal.trim(),
        currentLevel: currentLevel.trim(),
        targetOutcome: targetOutcome.trim(),
        successCriteria: successCriteria.trim(),
        kbId,
        hours,
        totalDays,
      },
    });
    setTimeline([]);
    setInterruptData(null);
    setFlowError(null);
    setNotice(null);
    selectPlan(null);
    setPhase("streaming");
    void consume("/api/plans", {
      goal: goal.trim(),
      current_level: currentLevel.trim() || undefined,
      target_outcome: targetOutcome.trim(),
      success_criteria: successCriteria.trim(),
      kb_id: kbId || undefined,
      budget_hours_per_day: hours,
      total_days: totalDays,
    });
  };

  const resume = (approved: boolean, plan?: PlanStructure) => {
    if (!interruptData) return;
    const pending = readPendingPlan(userId);
    writePendingPlan(userId, {
      status: "resuming",
      interrupt: interruptData,
      startedAt: pending?.startedAt ?? Date.now(),
      form: {
        goal,
        currentLevel,
        targetOutcome,
        successCriteria,
        kbId,
        hours,
        totalDays,
      },
    });
    setPhase("resuming");
    // 编辑版随 approved 一起回传; 服务端 confirm_plan 会重跑硬校验并以其结果为准
    void consume(interruptData.resume_url, {
      value: approved ? { approved: true, ...(plan ? { plan } : {}) } : { approved: false },
    });
  };

  const busy = phase === "streaming" || phase === "resuming";
  const goalForm = { goal, currentLevel, targetOutcome, successCriteria };
  const goalReady = isPlanGoalReady(goalForm);
  const goalReadiness = planGoalReadiness(goalForm);

  return (
    <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      <PageHeader
        title="学习计划"
        subtitle="AI 规划分阶段每日任务；计划是活的——勾选完成、按学情插入补强任务"
        action={
          <button
            type="button"
            onClick={() => setShowCreate((value) => !value)}
            className="rounded-[8px] bg-brand-600 px-4 py-2 text-[12px] font-medium text-white transition-colors hover:bg-brand-800"
          >
            新建计划
          </button>
        }
      />

      {flowError && (
        <ErrorBanner message={flowError} onClose={() => setFlowError(null)} />
      )}
      {notice && (
        <div className="flex items-center justify-between rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-700">
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="text-amber-400 hover:text-amber-600"
            aria-label="关闭提示"
          >
            ✕
          </button>
        </div>
      )}

      <div className="space-y-4">
        {/* 左：发起 + 列表 */}
        <aside className="w-full shrink-0 space-y-4">
          {(showCreate || plans?.length === 0) && <Card title="制定学习计划">
            <form onSubmit={startPlan} className="space-y-3">
              <div className="rounded-xl border border-brand-100 bg-brand-50/40 p-3">
                <p className="text-xs font-medium text-ink">用一个可验证的目标开始</p>
                <p id="plan-goal-help" className="mt-1 text-[11px] leading-5 text-ink-muted">
                  写清“学什么 → 想做到什么 → 怎样算完成”。当前基础可留空，计划会先安排短基线任务。
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5" aria-label="目标示例">
                  {PLAN_GOAL_EXAMPLES.map((example) => (
                    <button
                      key={example.label}
                      type="button"
                      onClick={() => {
                        setGoal(example.goal);
                        setCurrentLevel(example.currentLevel);
                        setTargetOutcome(example.targetOutcome);
                        setSuccessCriteria(example.successCriteria);
                      }}
                      className="rounded-full border border-brand-200 bg-white px-2.5 py-1 text-[10px] font-medium text-brand-700 hover:bg-brand-50"
                    >
                      使用“{example.label}”示例
                    </button>
                  ))}
                </div>
              </div>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-500">
                  学习主题 <span className="text-rose-500">*</span>
                </span>
                <textarea
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  rows={2}
                  maxLength={2000}
                  required
                  aria-describedby="plan-goal-help"
                  placeholder="如：极限与导数基础"
                  className="w-full resize-none rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-500">
                  当前基础 <span className="font-normal text-slate-400">（可选）</span>
                </span>
                <input
                  value={currentLevel}
                  onChange={(e) => setCurrentLevel(e.target.value)}
                  maxLength={200}
                  placeholder="如：会基本代数运算，但极限概念不熟"
                  className="w-full rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-500">
                  期望成果 <span className="text-rose-500">*</span>
                </span>
                <textarea
                  value={targetOutcome}
                  onChange={(e) => setTargetOutcome(e.target.value)}
                  rows={2}
                  maxLength={500}
                  required
                  placeholder="如：能独立判断常见极限题型并完成基础导数计算"
                  className="w-full resize-none rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-500">
                  验收标准 <span className="text-rose-500">*</span>
                </span>
                <textarea
                  value={successCriteria}
                  onChange={(e) => setSuccessCriteria(e.target.value)}
                  rows={2}
                  maxLength={500}
                  required
                  placeholder="如：不看提示完成 10 道教材基础题，至少正确 8 道并能说明错因"
                  className="w-full resize-none rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                />
              </label>
              <div className="grid grid-cols-2 gap-1.5 text-[10px] sm:grid-cols-4" aria-label="目标完整度" aria-live="polite">
                {([
                  ["学习主题", goalReadiness.topic],
                  ["当前基础", goalReadiness.baseline],
                  ["期望成果", goalReadiness.outcome],
                  ["验收标准", goalReadiness.criteria],
                ] as const).map(([label, ready]) => (
                  <span key={label} className={`rounded-lg px-2 py-1.5 ${ready ? "bg-success-50 text-success-700" : "bg-surface-muted text-ink-muted"}`}>
                    {ready ? "✓" : "○"} {label}
                  </span>
                ))}
              </div>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-500">
                  绑定知识库（可选）
                </span>
                <select
                  value={kbId}
                  onChange={(e) => setKbId(e.target.value)}
                  className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                >
                  <option value="">不绑定</option>
                  {kbs.map((kb) => (
                    <option key={kb.id} value={kb.id}>
                      {kb.name}
                    </option>
                  ))}
                </select>
              </label>
              <div className="grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-slate-500">
                    每日小时
                  </span>
                  <input
                    type="number"
                    min={0.5}
                    max={12}
                    step={0.5}
                    value={hours}
                    onChange={(e) =>
                      setHours(Math.max(0.5, Math.min(12, Number(e.target.value) || 0.5)))
                    }
                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-slate-500">
                    总天数
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={90}
                    value={totalDays}
                    onChange={(e) =>
                      setTotalDays(
                        Math.max(1, Math.min(90, Math.trunc(Number(e.target.value) || 1))),
                      )
                    }
                    className="w-full rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  />
                </label>
              </div>
              <button
                type="submit"
                disabled={!goalReady || busy || phase === "await_confirm"}
                className="flex w-full items-center justify-center gap-2 rounded-[8px] bg-brand-600 py-2.5 text-[12px] font-medium text-white hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy && <Spinner className="h-4 w-4 border-white/40 border-t-white" />}
                {busy ? "正在规划…" : "生成计划草案"}
              </button>
              {!goalReady && (
                <p className="text-center text-[10px] text-ink-muted">
                  请至少补齐学习主题、期望成果和验收标准。
                </p>
              )}
            </form>
          </Card>}

          <Card title="我的计划">
            {listError && (
              <div className="mb-2">
                <ErrorBanner message={listError} onRetry={() => void loadPlans()} />
              </div>
            )}
            {plans === null ? (
              <SkeletonList rows={3} />
            ) : plans.length === 0 ? (
              <p className="py-4 text-center text-xs text-slate-400">
                还没有学习计划
              </p>
            ) : (
              <ul className="flex gap-2 overflow-x-auto pb-1">
                {plans.map((p) => (
                  <li key={p.id} className="min-w-[240px] flex-1">
                    <button
                      type="button"
                      onClick={() => selectPlan(p)}
                      className={`w-full rounded-[9px] border px-3 py-2 text-left text-[12px] transition-colors ${
                        selected?.id === p.id
                          ? "border-brand-300 bg-brand-50 text-brand-700"
                          : "border-line bg-white text-ink-secondary hover:border-brand-200"
                      }`}
                    >
                      <span className="block truncate font-medium">{p.goal}</span>
                      <span className="block text-xs font-normal text-slate-400">
                        {p.is_current ? "当前联动计划 · " : ""}每日 {p.budget_hours} 小时 · {fmtDateTime(p.created_at)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </aside>

        {/* 右：流程 / 日历 */}
        <div className="min-w-0 flex-1 space-y-4">
          {(timeline.length > 0 || busy) && (
            <FlowTimeline items={timeline} streaming={busy} />
          )}

          {phase === "await_confirm" && interruptData?.plan && (
            <PlanConfirmCard
              plan={interruptData.plan}
              resources={interruptData.resources ?? []}
              goalAnalysis={interruptData.goal_analysis}
              skillGaps={interruptData.skill_gaps}
              onConfirm={(edited) => resume(true, edited)}
              onCancel={() => resume(false)}
            />
          )}
          {phase === "resuming" && (
            <p className="flex items-center gap-2 text-sm text-slate-500">
              <Spinner /> 正在保存计划…
            </p>
          )}

          {taskBanner && (
            <ErrorBanner message={taskBanner} onClose={() => setTaskBanner(null)} />
          )}

          {selected ? (
            <>
              <Card
                title={
                  <span className="block max-w-100 truncate">{selected.goal}</span>
                }
                extra={
                  <span className="text-xs text-slate-400">
                    {selected.is_current ? "当前联动计划 · " : ""}每日 {selected.budget_hours} 小时
                  </span>
                }
              >
                {/* 计划头部：任务粒度进度（分段式，每阶段一段）+ 学情调整入口 */}
                <div className="mb-3 flex flex-wrap items-center gap-3">
                  <div className="min-w-40 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-xs font-medium text-slate-500">
                        已完成 {detail?.progress.done ?? 0} / {detail?.progress.total ?? 0} 个任务
                        {(detail?.progress.skipped ?? 0) > 0 &&
                          ` · 跳过 ${detail?.progress.skipped ?? 0} 个`}
                      </span>
                      <span className="font-mono text-xs text-slate-500">
                        {fmtPct(detail?.progress.effective_ratio ?? detail?.progress.ratio ?? 0)}
                      </span>
                    </div>
                    {detail && detail.structure.stages.length > 0 ? (
                      <div className="mt-1 flex h-2 gap-0.5 overflow-hidden rounded-full">
                        {detail.structure.stages.map((stage, si) => {
                          const tasks = (stage.days ?? []).flatMap(
                            (d) => d.tasks ?? [],
                          );
                          const doneCount = tasks.filter(
                            (task) => normalizedTaskStatus(task) === "done",
                          ).length;
                          const skippedCount = tasks.filter(
                            (task) => normalizedTaskStatus(task) === "skipped",
                          ).length;
                          const effectiveTotal = tasks.length - skippedCount;
                          const color = STAGE_COLORS[si % STAGE_COLORS.length];
                          return (
                            <div
                              key={si}
                              className="h-full min-w-2 bg-slate-100"
                              style={{ flexGrow: Math.max(tasks.length, 1) }}
                              title={`${stage.name}：完成 ${doneCount} 个，跳过 ${skippedCount} 个`}
                            >
                              <div
                                className={`h-full ${color.bar} transition-all duration-300`}
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
                            width: `${Math.round((detail?.progress.effective_ratio ?? detail?.progress.ratio ?? 0) * 100)}%`,
                          }}
                        />
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    disabled={!detail || adjusting}
                    onClick={() => void runAdjust()}
                    title="按当前掌握度（p<0.4 的知识点）在最近未完成日插入补强任务"
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
                    <p className="flex items-start justify-between gap-2 text-slate-700">
                      <span className="min-w-0"><MathText text={adjustResult.reason} /></span>
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
                    </p>
                    {adjustResult.inserted.length > 0 && (
                      <ul className="mt-2 space-y-1">
                        {adjustResult.inserted.map((t) => (
                          <li
                            key={t.task_key}
                            className="flex flex-wrap items-center gap-2 rounded-lg bg-white px-2.5 py-1.5 text-xs"
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

                {detail?.structure.goal_analysis && (
                  <details className="mb-3 rounded-xl border border-brand-100 bg-brand-50/30 px-3.5 py-3">
                    <summary className="cursor-pointer text-xs font-medium text-brand-700">
                      查看确认时的目标与验收标准
                    </summary>
                    <p className="mt-2 text-xs leading-5 text-ink-secondary">
                      {detail.structure.goal_analysis.summary}
                    </p>
                    <div className="mt-2 grid gap-2 text-[11px] sm:grid-cols-2">
                      <div className="rounded-lg bg-white px-2.5 py-2 text-ink-secondary">
                        <strong className="block text-[10px] text-ink-muted">可验证成果</strong>
                        {detail.structure.goal_analysis.outcomes.map((item) => (
                          <p key={item} className="mt-1">• {item}</p>
                        ))}
                      </div>
                      <div className="rounded-lg bg-white px-2.5 py-2 text-ink-secondary">
                        <strong className="block text-[10px] text-ink-muted">验收标准</strong>
                        {detail.structure.goal_analysis.success_criteria.map((item) => (
                          <p key={item} className="mt-1">• {item}</p>
                        ))}
                      </div>
                    </div>
                    {detail.structure.skill_gaps && detail.structure.skill_gaps.length > 0 && (
                      <p className="mt-2 text-[11px] text-warning-700">
                        创建时优先补强：{detail.structure.skill_gaps.map((gap) => `${gap.skill}（${gap.zone}）`).join("、")}
                      </p>
                    )}
                  </details>
                )}

                {detailError && (
                  <div className="mb-3">
                    <ErrorBanner
                      message={detailError}
                      onRetry={() => setDetailKey((k) => k + 1)}
                    />
                  </div>
                )}

                {detail ? (
                  <PlanCalendar
                    key={detail.id}
                    structure={detail.structure}
                    onStatus={(key, status) => void updateTaskStatus(key, status)}
                    onTaskOpen={(location) => setOpenTask({ ...location, planId: detail.id, planKbId: detail.kb_id })}
                    pendingKeys={pendingKeys}
                    highlightKeys={highlightKeys}
                  />
                ) : detailError ? (
                  <EmptyState title="该计划缺少结构数据" illustration="map" />
                ) : (
                  <SkeletonList rows={4} />
                )}
              </Card>

              {openTask && <PlanTaskDrawer location={openTask} onClose={() => setOpenTask(null)} onCompleted={() => setDetailKey((key) => key + 1)} />}

              <Card
                title="调整历史"
                extra={
                  <span className="text-xs text-slate-400">
                    共 {adjustments.length} 次
                  </span>
                }
              >
                {adjustments.length === 0 ? (
                  <p className="py-3 text-center text-xs text-slate-400">
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
                              <span className="text-xs text-slate-400">
                                {fmtDateTime(a.at)}
                              </span>
                              <span
                                className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                                  auto
                                    ? "bg-amber-100/70 text-amber-700"
                                    : "bg-indigo-50 text-indigo-600"
                                }`}
                              >
                                {TRIGGER_LABELS[a.trigger] ?? a.trigger}
                              </span>
                              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700">
                                插入 {a.inserted.length} 个任务
                              </span>
                            </p>
                            <div className="mt-1 text-sm leading-relaxed text-slate-700">
                              <MathText text={a.reason} />
                            </div>
                          </div>
                        </li>
                      );
                    })}
                  </ol>
                )}
              </Card>
            </>
          ) : (
            phase === "idle" &&
            timeline.length === 0 && (
              <EmptyState
                title="选择一个计划查看日历"
                description="或在左侧填写目标，AI 将规划分阶段的每日任务"
                illustration="map"
              />
            )
          )}
        </div>
      </div>
    </div>
  );
}
