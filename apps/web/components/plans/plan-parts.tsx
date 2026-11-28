"use client";

// 学习计划（F3）：发起表单 → SSE 流 + interrupt 计划确认（阶段树预览）→ 确认落库；
// 计划列表 + 日历视图（stages/days/tasks 渲染为周网格卡片）。
// 「计划是活的」：任务复选框（乐观更新+失败回滚）、头部进度条、
// 「根据学情调整计划」插入补强任务并高亮、调整历史时间线。

import {useState} from "react";
import { LockKeyhole } from "lucide-react";

import {EmptyState} from "@/components/common";

import {type PlanTaskLocation} from "@/components/plans/plan-task-drawer";

import { MathText } from "@/lib/katex";
import type {GraphInterruptData, PlanDetail, PlanGoalAnalysis, PlanMode, PlanSkillGap, PlanStructure, PlanTask, PlanResource, PlanToggleResult} from "@/lib/types";

export type Phase = "idle" | "streaming" | "await_confirm" | "resuming";
export const PENDING_PLAN_KEY = "suwen:pending-plan-draft";

export interface PendingPlanDraft {
  thread_id?: string;
  status: "generating" | "await_confirm" | "resuming" | "interrupted";
  interrupt?: GraphInterruptData;
  editedPlan?: PlanStructure;
  startedAt: number;
  form: {
    goal: string;
    currentLevel: string;
    targetOutcome: string;
    successCriteria: string;
    planMode: PlanMode;
    kbId: string;
    hours: number | null;
    totalDays: number | null;
  };
}

export function draftTaskKey(plan?: PlanStructure): string {
  return plan?.stages.flatMap((stage) => stage.days.flatMap((day) => day.tasks.map((task) => task.task_id))).join(",") ?? "";
}

export function pendingPlanKey(userId: string): string {
  return `${PENDING_PLAN_KEY}:${userId}`;
}

export function readPendingPlan(userId: string): PendingPlanDraft | null {
  try {
    const key = pendingPlanKey(userId);
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PendingPlanDraft>;
    const validStatus = ["generating", "await_confirm", "resuming", "interrupted"].includes(
      value.status ?? "",
    );
    const retention = value.thread_id ? 7 * 24 * 60 * 60 * 1000 : 30 * 60 * 1000;
    const stale = !value.startedAt || Date.now() - value.startedAt > retention;
    const invalidInterrupt =
      (value.status === "await_confirm" || value.status === "resuming") &&
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

export function clearPendingPlan(userId: string): void {
  window.localStorage.removeItem(pendingPlanKey(userId));
}

export function writePendingPlan(userId: string, value: PendingPlanDraft): void {
  window.localStorage.setItem(pendingPlanKey(userId), JSON.stringify(value));
}

export const emptySubscribe = () => () => {};

export const TRIGGER_LABELS: Record<string, string> = {
  manual: "手动调整",
  low_score_attempt: "低分测评触发",
};

export const MISSING_GOAL_LABELS: Record<string, string> = {
  scope: "学习范围",
  current_level: "当前基础",
  target_outcome: "期望成果",
  success_criteria: "验收标准",
};

/** 阶段配色（固定顺序分配，不随过滤变化）。 */
export const STAGE_COLORS = [
  { chip: "bg-success-50 text-success-700", bar: "bg-success-500" },
  { chip: "bg-brand-50 text-brand-700", bar: "bg-brand-600" },
  { chip: "bg-surface-muted text-ink-muted", bar: "bg-line-strong" },
  { chip: "bg-surface-muted text-ink-muted", bar: "bg-line-strong" },
  { chip: "bg-surface-muted text-ink-muted", bar: "bg-line-strong" },
];

export interface FlatDay {
  day: number;
  stageIdx: number;
  stageName: string;
  tasks: PlanStructure["stages"][number]["days"][number]["tasks"];
}

export function flattenDays(structure: PlanStructure | null): FlatDay[] {
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

export type PlanTaskStatus = NonNullable<PlanTask["status"]>;

export function normalizedTaskStatus(task: PlanTask): PlanTaskStatus {
  return task.status;
}

export function taskIsPending(task: PlanTask): boolean {
  return normalizedTaskStatus(task) === "pending";
}

/** 乐观更新：生成带新三态的 detail 副本，并完整重算两种进度口径。 */
export function withTaskStatus(
  detail: PlanDetail,
  taskId: string,
  status: PlanTaskStatus,
): PlanDetail {
  const stages = detail.structure.stages.map((stage) => ({
    ...stage,
    days: (stage.days ?? []).map((day) => ({
      ...day,
      tasks: (day.tasks ?? []).map((task) => {
        if (task.task_id !== taskId) return task;
        return { ...task, status };
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

/** Keep newer server data and overlay only the tasks still being submitted. */
export function mergePlanSnapshot(
  previous: PlanDetail | null,
  incoming: PlanDetail,
  pending: ReadonlyMap<string, PlanTaskStatus>,
): PlanDetail {
  if (previous?.id === incoming.id && incoming.version < previous.version) return previous;
  let next = incoming;
  if (previous?.id === incoming.id) {
    for (const [taskId, status] of pending) next = withTaskStatus(next, taskId, status);
  }
  return next;
}

export function withTaskResult(
  previous: PlanDetail,
  result: PlanToggleResult,
  hasOtherPending: boolean,
): PlanDetail {
  if (result.version < previous.version) return previous;
  return {
    ...withTaskStatus(previous, result.task_id, result.status),
    ...(hasOtherPending ? {} : { progress: result.progress }),
    version: result.version,
    status: result.plan_status,
  };
}

export function TaskLockBadge({ task }: { task: PlanTask }) {
  const blockers = task.lock_blockers ?? [];
  if (!task.locked || blockers.length === 0) return null;
  // 服务端只下发定性区间（掌握度小数不出境），这里原样展示。
  const detail = blockers.map((b) => `${b.kc_name}（${b.zone}）`).join("、");
  return (
    <span
      title={`需先掌握：${detail}`}
      className="inline-flex items-center gap-1 rounded bg-surface-muted px-1 py-0.5 font-medium text-ink-secondary"
    >
      <LockKeyhole aria-hidden="true" size={12} strokeWidth={2} />
      需先掌握 {blockers[0].kc_name}
      {blockers.length > 1 ? ` 等 ${blockers.length} 项` : ""}
    </span>
  );
}

export function TaskStatusControls({
  task,
  taskId,
  busy,
  disabled,
  compact = false,
  onStatus,
}: {
  task: PlanTask;
  taskId?: string;
  busy: boolean;
  disabled: boolean;
  compact?: boolean;
  onStatus?: (taskId: string, status: PlanTaskStatus) => void;
}) {
  const status = normalizedTaskStatus(task);
  const unavailable = disabled || !taskId || busy;
  return (
    <div className="flex shrink-0 items-center gap-1">
      <input
        type="checkbox"
        checked={status === "done"}
        disabled={unavailable}
        onChange={() =>
          taskId && onStatus?.(taskId, status === "done" ? "pending" : "done")
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
          taskId && onStatus?.(taskId, status === "skipped" ? "pending" : "skipped")
        }
        className={`rounded px-1.5 py-0.5 text-caption font-medium disabled:cursor-not-allowed disabled:opacity-40 ${
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
/** 今日任务卡（UI 重构阶段 4 自 PlanCalendar 抽离的共享组件）：今日页与计划页
 *  渲染同一份，checkbox/跳过/锁徽章/补强高亮/预计分钟全部沿用。 */
export function PlanTodayCard({
  structure,
  currentDayNumber,
  onStatus,
  onTaskOpen,
  pendingKeys,
  highlightKeys,
}: {
  structure: PlanStructure;
  /** 真实日历日号（schedule.current_day）；给定则优先于"首个待办日"启发式，落后时不再把旧日冒充今日。 */
  currentDayNumber?: number;
  onStatus?: (taskId: string, status: PlanTaskStatus) => void;
  onTaskOpen?: (location: Omit<PlanTaskLocation, "planKbId">) => void;
  pendingKeys?: Set<string>;
  highlightKeys?: Set<string>;
}) {
  const days = flattenDays(structure);
  if (days.length === 0) return null;
  const firstPendingIndex = days.findIndex((day) => day.tasks.some(taskIsPending));
  const heuristicIndex = firstPendingIndex >= 0 ? firstPendingIndex : days.length - 1;
  const calendarIndex =
    currentDayNumber != null ? days.findIndex((day) => day.day === currentDayNumber) : -1;
  const lastDayNumber = days.at(-1)?.day ?? 0;
  const pastSchedule = currentDayNumber != null && currentDayNumber > lastDayNumber;
  const missingCalendarDay = currentDayNumber != null && calendarIndex < 0 && !pastSchedule;
  if (missingCalendarDay) {
    return (
      <section className="overflow-hidden rounded-overlay border border-line bg-white">
        <header className="border-b border-line px-4 py-3">
          <h3 className="text-[13px] font-semibold text-ink">
            今日任务 <span className="ml-1 font-normal text-ink-muted">第 {currentDayNumber} 天</span>
          </h3>
        </header>
        <p className="px-4 py-6 text-center text-body-sm text-ink-muted">
          今天没有安排计划任务；可以按上方推荐复习或练习。
        </p>
      </section>
    );
  }
  if (pastSchedule && firstPendingIndex < 0) {
    return (
      <section className="overflow-hidden rounded-overlay border border-line bg-white">
        <header className="border-b border-line px-4 py-3">
          <h3 className="text-[13px] font-semibold text-ink">计划日程已结束</h3>
        </header>
        <p className="px-4 py-6 text-center text-body-sm text-ink-muted">
          日程内没有待补任务，可以查看成长变化或新建下一份计划。
        </p>
      </section>
    );
  }
  const currentIndex = calendarIndex >= 0 ? calendarIndex : heuristicIndex;
  const currentDay = days[currentIndex] ?? days[0];
  const cardTitle = pastSchedule ? "逾期待补" : "今日任务";
  const footerLabel = pastSchedule ? "本次待补预计" : "今日预计";
  return (
    <section className="overflow-hidden rounded-overlay border border-line bg-white">
      <header className="flex items-center justify-between border-b border-line px-4 py-3"><h3 className="text-[13px] font-semibold text-ink">{cardTitle} <span className="ml-1 font-normal text-ink-muted">{pastSchedule ? `原第 ${currentDay.day} 天` : `第 ${currentDay.day} 天`}</span></h3><span className="text-caption text-ink-muted">{currentDay.tasks.filter((task) => normalizedTaskStatus(task) === "done").length} 完成 · {currentDay.tasks.filter((task) => normalizedTaskStatus(task) === "skipped").length} 跳过</span></header>
      <ul className="divide-y divide-line">
        {currentDay.tasks.map((task, index) => {
          const key = task.task_id;
          const status = normalizedTaskStatus(task);
          const busy = Boolean(key && pendingKeys?.has(key));
          const isNew = Boolean(key && highlightKeys?.has(key));
          return <li key={key ?? index} className={`flex items-center gap-3 px-4 py-3 ${isNew ? "bg-warning-50" : ""}`}><TaskStatusControls task={task} taskId={key} busy={busy} disabled={!onStatus} onStatus={onStatus} /><div className="min-w-0 flex-1"><button type="button" onClick={() => onTaskOpen?.({ task, stageName: currentDay.stageName, day: currentDay.day })} className={`block max-w-full truncate text-left text-body-sm hover:text-brand-700 ${status === "done" ? "text-ink-muted line-through" : status === "skipped" || task.locked ? "text-ink-muted" : "font-medium text-ink"}`}><MathText text={task.title} singleLine /></button><p className="mt-1 flex gap-2 text-body-sm text-ink-muted"><span>{task.kc_names[0] || currentDay.stageName}</span>{isNew && <span className="font-medium text-warning-600">补强</span>}<TaskLockBadge task={task} /></p></div><span className="text-body-sm text-ink-muted">{task.minutes} 分钟</span></li>;
        })}
      </ul>
      <p className="border-t border-line px-4 py-2.5 text-caption text-ink-muted">{footerLabel} {currentDay.tasks.reduce((sum, task) => sum + (task.minutes || 0), 0)} 分钟</p>
    </section>
  );
}

export function PlanCalendar({
  structure,
  currentDayNumber,
  onStatus,
  onTaskOpen,
  pendingKeys,
  highlightKeys,
}: {
  structure: PlanStructure;
  /** 真实日历日号（schedule.current_day）；给定则优先于"首个待办日"启发式。 */
  currentDayNumber?: number;
  onStatus?: (taskId: string, status: PlanTaskStatus) => void;
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
  const heuristicIndex = firstPendingIndex >= 0 ? firstPendingIndex : days.length - 1;
  const calendarIndex =
    currentDayNumber != null ? days.findIndex((day) => day.day === currentDayNumber) : -1;
  const nextCalendarIndex =
    currentDayNumber != null ? days.findIndex((day) => day.day > currentDayNumber) : -1;
  const pastSchedule = currentDayNumber != null && currentDayNumber > (days.at(-1)?.day ?? 0);
  const currentIndex =
    calendarIndex >= 0
      ? calendarIndex
      : nextCalendarIndex >= 0
        ? nextCalendarIndex
        : pastSchedule
          ? Math.max(0, days.length - 7)
          : heuristicIndex;
  const currentDay = calendarIndex >= 0 ? days[calendarIndex] : null;
  const upcoming = days.slice(currentIndex, currentIndex + 7);
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] lg:items-start">
        <PlanTodayCard structure={structure} currentDayNumber={currentDayNumber} onStatus={onStatus} onTaskOpen={onTaskOpen} pendingKeys={pendingKeys} highlightKeys={highlightKeys} />

        <section className="overflow-hidden rounded-overlay border border-line bg-white">
        <header className="flex items-center justify-between border-b border-line px-4 py-3"><h3 className="text-[13px] font-semibold text-ink">{pastSchedule ? "最近日程" : "近期日程"} <span className="ml-1 font-normal text-ink-muted">第 {upcoming[0]?.day ?? 1}–{upcoming.at(-1)?.day ?? 1} 天</span></h3><button type="button" aria-expanded={showFull} onClick={() => setShowFull((value) => !value)} className="text-body-sm font-medium text-brand-700 hover:text-brand-800">{showFull ? "收起完整计划" : `查看完整计划（${days.length} 天）`}</button></header>
        <ol className="divide-y divide-line">{upcoming.map((day) => { const done = day.tasks.filter((task) => normalizedTaskStatus(task) === "done").length; const skipped = day.tasks.filter((task) => normalizedTaskStatus(task) === "skipped").length; const handled = done + skipped; const totalMinutes = day.tasks.reduce((sum, task) => sum + (task.minutes || 0), 0); const isToday = day.day === currentDay?.day; return <li key={day.day} className={`grid grid-cols-[32px_minmax(0,1fr)_auto] items-center gap-3 px-4 py-3 ${isToday ? "bg-brand-50/50" : ""}`}><strong className={`text-[13px] ${isToday ? "text-brand-700" : "text-ink-secondary"}`}>{day.day}<span className="block text-caption font-normal text-ink-muted">天</span></strong><div className="min-w-0"><p className="truncate text-body-sm font-medium text-ink">{day.stageName}</p><p className="mt-0.5 text-caption text-ink-muted">{day.tasks.length} 个任务 · {totalMinutes} 分钟{skipped > 0 ? ` · ${skipped} 个跳过` : ""}</p></div><span className={`text-caption ${handled === day.tasks.length ? "text-success-600" : isToday ? "text-brand-700" : "text-ink-muted"}`}>{handled === day.tasks.length ? `✓ ${done} 完成` : isToday ? `${done}/${day.tasks.length - skipped} 进行中` : pastSchedule ? "已逾期" : "待开始"}</span></li>; })}</ol>
        </section>
      </div>

      {showFull && (
        <section className="rounded-overlay border border-line bg-surface-muted/40 p-4" aria-label="完整学习计划">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-[13px] font-semibold text-ink">完整计划</h3>
              <p className="mt-0.5 text-caption text-ink-muted">共 {structure.stages.length} 个阶段、{days.length} 天；可直接勾选任意一天的任务</p>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {structure.stages.map((stage, index) => {
                const color = STAGE_COLORS[index % STAGE_COLORS.length];
                return <span key={`${stage.name}-${index}`} className={`rounded-full px-2.5 py-1 text-caption font-medium ${color.chip}`}><span className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full ${color.bar}`} />{stage.name} · {stage.days?.length ?? 0} 天</span>;
              })}
            </div>
          </div>
          <div className="space-y-5">
            {structure.stages.map((stage, stageIndex) => {
              const color = STAGE_COLORS[stageIndex % STAGE_COLORS.length];
              return (
                <div key={`${stage.name}-${stageIndex}`}>
                  <h4 className="mb-2 flex items-center gap-2 text-body-sm font-semibold text-ink"><span className={`h-2 w-2 rounded-full ${color.bar}`} />{stage.name}</h4>
                  <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                    {(stage.days ?? []).map((day) => {
                      const totalMinutes = day.tasks.reduce((sum, task) => sum + (task.minutes || 0), 0);
                      return (
                        <article key={day.day} className={`overflow-hidden rounded-control border bg-white ${day.day === currentDay?.day ? "border-brand-300 ring-1 ring-brand-100" : "border-line"}`}>
                          <header className="flex items-center justify-between border-b border-line px-3 py-2"><strong className="text-body-sm text-ink">第 {day.day} 天</strong><span className="text-caption text-ink-muted">{totalMinutes} 分钟</span></header>
                          <ul className="divide-y divide-line/70">
                            {day.tasks.map((task, taskIndex) => {
                              const key = task.task_id;
                              const status = normalizedTaskStatus(task);
                              const busy = Boolean(key && pendingKeys?.has(key));
                              const isNew = Boolean(key && highlightKeys?.has(key));
                              return (
                                <li key={key ?? taskIndex} className={`flex items-start gap-2 px-3 py-2.5 ${isNew ? "bg-warning-50" : ""}`}>
                                  <TaskStatusControls task={task} taskId={key} busy={busy} disabled={!onStatus} compact onStatus={onStatus} />
                                  <div className="min-w-0 flex-1"><button type="button" onClick={() => onTaskOpen?.({ task, stageName: stage.name, day: day.day })} className={`block text-left text-body-sm leading-relaxed hover:text-brand-700 ${status === "done" ? "text-ink-muted line-through" : status === "skipped" || task.locked ? "text-ink-muted" : "text-ink"}`}><MathText text={task.title} /></button><p className="mt-1 flex flex-wrap gap-1 text-body-sm text-ink-muted"><span>{task.minutes} 分钟</span>{task.kc_names.map((name) => <span key={name} className={`rounded px-1 py-0.5 ${color.chip}`}>{name}</span>)}{status === "skipped" && <span>已跳过</span>}{isNew && <span className="font-medium text-warning-600">补强任务</span>}<TaskLockBadge task={task} /></p></div>
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
export function PlanConfirmCard({
  plan,
  initialEdits,
  resources,
  planMode,
  goalAnalysis,
  skillGaps,
  onConfirm,
  onCancel,
}: {
  plan: PlanStructure;
  resources: PlanResource[];
  planMode: PlanMode;
  goalAnalysis?: PlanGoalAnalysis;
  skillGaps?: PlanSkillGap[];
  initialEdits?: PlanStructure;
  onConfirm: (edited: PlanStructure) => void;
  onCancel: () => void;
}) {
  // 本地草案副本：编辑不影响 interrupt 原始负载（取消后重新打开仍是原稿）
  const [draft, setDraft] = useState<PlanStructure>(() =>
    structuredClone(initialEdits ?? plan),
  );
  const [touched, setTouched] = useState(Boolean(initialEdits));
  const [showAllDays, setShowAllDays] = useState(false);

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
    "rounded-lg border border-line bg-white px-1.5 py-0.5 text-body-sm text-ink-muted transition-colors hover:border-indigo-300 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-30";

  return (
    <div className="rounded-overlay border border-line bg-white p-5">
      <p className="text-caption font-medium text-brand-700">草案确认</p>
      <h3 className="mt-2 text-[22px] font-semibold tracking-[-0.03em] text-ink">{totalDays} 天学习计划</h3>
      <p className="mt-0.5 text-caption text-ink-muted">
        共 {draft.stages.length} 个阶段 / {totalDays} 天 / {totalTasks} 个任务
        {resources.length > 0 && ` · 关联 ${resources.length} 条学习资源`}
        ，可直接修改任务标题与时长，确认后才会保存。
      </p>
      <div className={`mt-3 rounded-xl border px-3 py-2.5 ${planMode === "course_bound" ? "border-success-200 bg-success-50" : "border-warning-200 bg-warning-50"}`}>
        <p className={`text-body-sm font-semibold ${planMode === "course_bound" ? "text-success-800" : "text-warning-800"}`}>
          {planMode === "course_bound" ? "课程资料计划" : "目标起步计划"}
        </p>
        <p className={`mt-1 text-caption leading-5 ${planMode === "course_bound" ? "text-success-700" : "text-warning-700"}`}>
          {planMode === "course_bound"
            ? resources.length > 0
              ? "草案引用了所选课程中的真实资料，确认后练习可进入该课程的正式学习证据链。"
              : "当前未检索到可引用资料，确认后仍会保留课程范围，但阅读任务会改为辅导或练习。"
            : resources.some((item) => item.type === "web")
              ? "草案可参考公开网页，但概念仍是临时的；练习只校准起点，不更新正式掌握度。"
              : "草案没有资料来源；系统不会伪造阅读任务或引用，练习只校准起点。"}
        </p>
      </div>

      {goalAnalysis && (
        <section className="mt-4 rounded-xl border border-brand-100 bg-brand-50/40 p-3" aria-label="目标拆解">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-caption font-semibold text-ink">目标拆解</h4>
            <span
              className={`rounded-full px-2 py-0.5 text-caption font-medium ${
                goalAnalysis.quality === "clear"
                  ? "bg-success-50 text-success-700"
                  : "bg-warning-50 text-warning-700"
              }`}
            >
              {goalAnalysis.quality === "clear" ? "信息完整" : "建议后续补充"}
            </span>
          </div>
          <p className="mt-2 text-caption leading-5 text-ink-secondary">
            {goalAnalysis.summary}
          </p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <div className="rounded-lg bg-white px-2.5 py-2">
              <p className="text-caption font-medium text-ink-muted">可验证成果</p>
              {goalAnalysis.outcomes.length > 0 ? (
                <ul className="mt-1 space-y-1 text-body-sm leading-4 text-ink-secondary">
                  {goalAnalysis.outcomes.map((item) => <li key={item}>• {item}</li>)}
                </ul>
              ) : <p className="mt-1 text-body-sm text-warning-700">尚未明确</p>}
            </div>
            <div className="rounded-lg bg-white px-2.5 py-2">
              <p className="text-caption font-medium text-ink-muted">验收标准</p>
              {goalAnalysis.success_criteria.length > 0 ? (
                <ul className="mt-1 space-y-1 text-body-sm leading-4 text-ink-secondary">
                  {goalAnalysis.success_criteria.map((item) => <li key={item}>• {item}</li>)}
                </ul>
              ) : <p className="mt-1 text-body-sm text-warning-700">未提供，系统不会自行编造</p>}
            </div>
          </div>
          {goalAnalysis.missing_information.length > 0 && (
            <p className="mt-2 text-body-sm text-warning-700">
              待明确：{goalAnalysis.missing_information.map((item) => MISSING_GOAL_LABELS[item] ?? item).join("、")}。计划已在前段安排基线任务，也可返回补充后重新生成。
            </p>
          )}
          {skillGaps && skillGaps.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5" aria-label="优先补强技能">
              {skillGaps.map((gap) => (
                <span key={`${gap.skill}-${gap.zone}`} className="rounded-full border border-warning-200 bg-white px-2 py-1 text-caption text-warning-700">
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
              <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
                <span className={`h-2 w-2 rounded-full ${color.bar}`} />
                {stage.name}
                <span className="text-caption font-normal text-ink-muted">
                  {stage.days?.length ?? 0} 天 · 约 {Math.round(minutes / 60)} 小时
                </span>
              </p>
              <ul className="mt-2 space-y-2">
                {(stage.days ?? []).map((d, di) => !showAllDays && d.day > 3 ? null : (
                  <li key={d.day} className="rounded-lg bg-surface-muted/70 p-2">
                    <p className="mb-1 text-body-sm font-medium text-ink-muted">
                      第 {d.day} 天
                    </p>
                    {d.tasks.length === 0 ? (
                      <p className="px-1 text-body-sm text-amber-600">
                        当天已无任务，可恢复原草案后重新调整。
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
                                className={`min-w-0 flex-1 rounded-lg border px-2 py-1 text-caption outline-none transition-colors focus:ring-2 focus:ring-indigo-100 ${
                                  titleBad
                                    ? "border-rose-300 bg-rose-50"
                                    : "border-line focus:border-indigo-300"
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
                                className={`w-16 shrink-0 rounded-lg border px-1.5 py-1 text-right text-caption outline-none transition-colors focus:ring-2 focus:ring-indigo-100 ${
                                  minutesBad
                                    ? "border-rose-300 bg-rose-50"
                                    : "border-line focus:border-indigo-300"
                                }`}
                              />
                              <span className="shrink-0 text-body-sm text-ink-muted">分钟</span>
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
                                  className="rounded-lg border border-line bg-white px-1.5 py-0.5 text-body-sm text-ink-muted transition-colors hover:border-rose-300 hover:text-rose-600"
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
      {totalDays > 3 && (
        <button type="button" aria-expanded={showAllDays} onClick={() => setShowAllDays((value) => !value)} className="mt-3 rounded-lg border border-line-strong bg-white px-3 py-2 text-body-sm font-medium text-ink-secondary hover:bg-surface-muted">
          {showAllDays ? "只看前 3 天" : `展开其余 ${totalDays - 3} 天`}
        </button>
      )}

      {resources.length > 0 && (
        <details className="mt-3 rounded-xl border border-indigo-100 bg-white p-3">
          <summary className="cursor-pointer text-body-sm font-medium text-ink-secondary">
            学习资源（{resources.length}）
          </summary>
          <ul className="mt-1.5 space-y-0.5">
            {resources.map((r, i) => (
              <li key={i} className="truncate text-caption text-ink-muted">
                <span className="mr-1 rounded bg-surface-muted px-1 py-0.5 text-caption text-ink-muted">
                  {r.type === "kb" ? "知识库" : "网页"}
                </span>
                <MathText text={r.title} />
              </li>
            ))}
          </ul>
        </details>
      )}

      {(invalid || emptied) && (
        <p className="mt-2 rounded-xl bg-rose-50 px-3 py-2 text-caption text-rose-700">
          {emptied
            ? "计划不能全部删空，至少保留一个任务。"
            : "请修正标红项：任务标题不能为空，时长需为 1–1440 之间的整数分钟。"}
        </p>
      )}
      {touched && !invalid && !emptied && (
        <p className="mt-2 text-body-sm text-ink-muted">
          已修改草案；保存时服务端会重新校验总时长与先修顺序，并以校验后的版本为准。
        </p>
      )}

      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-control border border-line-strong bg-white px-4 py-2 text-body-sm font-medium text-ink-secondary hover:bg-surface-muted"
        >
          返回修改目标
        </button>
        {touched && (
          <button type="button" className="rounded-control border border-line px-3 py-2 text-body-sm text-ink-secondary"
            onClick={() => { setDraft(structuredClone(plan)); setTouched(false); }}>
            恢复原草案
          </button>
        )}
        <button
          type="button"
          disabled={invalid || emptied}
          onClick={() => onConfirm(draft)}
          className="rounded-control bg-brand-600 px-4 py-2 text-body-sm font-medium text-white transition-colors hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {touched ? "确认并保存修改后的计划" : "确认并保存计划"}
        </button>
      </div>
    </div>
  );
}
