"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { PlanTaskDrawer, type PlanTaskLocation } from "@/components/plans/plan-task-drawer";
import { buildTodayItems, taskExecutionHref, type FlatPlanTask } from "@/app/today/today-plan";
import { apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type {
  MasteryItem,
  PlanDetail,
  PlanListItem,
  PlanToggleResult,
  ProfileResponse,
  ReviewTodayResponse,
} from "@/lib/types";

type ScopedMastery = MasteryItem & { kb_name?: string | null; subject?: string | null };
interface DashboardData {
  plan: PlanDetail | null;
  reviews: ReviewTodayResponse | null;
  profile: ProfileResponse | null;
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4" aria-hidden>
      <path d="M4 10h12m-4-4 4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" className={`h-3 w-3 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden>
      <path d="m2 4 4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`overflow-hidden rounded-[14px] border border-line bg-white ${className}`}>{children}</section>;
}

function DashboardSkeleton() {
  return (
    <div className="mx-auto w-full max-w-[1480px] animate-pulse space-y-4 px-5 py-7 sm:px-8 lg:px-12">
      <div className="h-[280px] rounded-[14px] bg-white" />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="h-80 rounded-[14px] bg-white" />
        <div className="h-36 rounded-[14px] bg-white" />
      </div>
    </div>
  );
}

function EmptyToday() {
  return (
    <Panel className="px-8 py-16 text-center">
      <div className="mx-auto max-w-[460px]">
        <h2 className="text-[19px] font-semibold tracking-[-0.02em] text-ink">还没有开始学习</h2>
        <p className="mt-2 text-[13px] leading-6 text-ink-secondary">创建一个学习计划，苏问会陪你把每天的目标拆解清楚；也可以直接从知识库里的资料开始，边学边问。</p>
        <div className="mt-6 flex flex-wrap justify-center gap-2.5">
          <Link href="/plans" className="inline-flex h-10 items-center rounded-[8px] bg-ink px-5 text-[13px] font-semibold text-white transition-colors hover:bg-black">创建学习计划</Link>
          <Link href="/kb" className="inline-flex h-10 items-center rounded-[8px] border border-line-strong bg-white px-5 text-[13px] text-ink-secondary transition-colors hover:border-ink-muted hover:text-ink">从知识库开始</Link>
        </div>
      </div>
    </Panel>
  );
}

export default function TodayPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [togglingKey, setTogglingKey] = useState<string | null>(null);
  const [openTask, setOpenTask] = useState<PlanTaskLocation | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);
  const [todosExpanded, setTodosExpanded] = useState(true);
  const [reminderExpanded, setReminderExpanded] = useState(false);

  useEffect(() => {
    let alive = true;
    void (async () => {
      const [plansResult, reviewsResult, profileResult] = await Promise.allSettled([
        apiJson<PlanListItem[]>("/api/plans"),
        apiJson<ReviewTodayResponse>("/api/reviews/today"),
        apiJson<ProfileResponse>("/api/profile"),
      ]);
      if (!alive) return;
      const plans = plansResult.status === "fulfilled" ? plansResult.value : [];
      // /api/plans already marks the newest plan as current.  Do not derive
      // the active plan or its day from a browser-local Date/time zone.
      const latest = plans.find((item) => item.is_current) ?? plans[0];
      let plan: PlanDetail | null = null;
      if (latest) {
        try {
          plan = await apiJson<PlanDetail>(`/api/plans/${latest.id}`);
        } catch {
          plan = null;
        }
      }
      if (!alive) return;
      setData({
        plan,
        reviews: reviewsResult.status === "fulfilled" ? reviewsResult.value : null,
        profile: profileResult.status === "fulfilled" ? profileResult.value : null,
      });
    })();
    return () => { alive = false; };
  }, []);

  const view = useMemo(() => {
    if (!data) return null;
    const tasks: FlatPlanTask[] = (data.plan?.structure.stages ?? []).flatMap((stage) =>
      stage.days.flatMap((day) => day.tasks.map((task) => ({ ...task, day: day.day, stage: stage.name }))),
    );
    const today = buildTodayItems(tasks, data.plan?.schedule, data.reviews?.items ?? [], data.plan?.kb_id);
    const { nextTask } = today;
    const currentStage = nextTask?.stage ?? data.plan?.structure.stages[0]?.name ?? data.plan?.goal ?? "当前学习";
    const stageTasks = tasks.filter((task) => task.stage === currentStage).slice(0, 4);
    const stageDone = stageTasks.filter((task) => task.done).length;
    const stagePct = stageTasks.length ? Math.round((stageDone / stageTasks.length) * 100) : 0;
    const topic = nextTask?.kc_names?.[0] || nextTask?.title || data.plan?.goal || "";
    const tutorHref = nextTask ? taskExecutionHref({ ...nextTask, action_type: "tutor" }, data.plan?.kb_id) : `/tutor?problem=${encodeURIComponent(data.plan?.goal || "请描述你正在学习的问题")}`;
    const quizHref = nextTask ? taskExecutionHref({ ...nextTask, action_type: "practice" }, data.plan?.kb_id) : "/quiz";
    const mastery = (data.profile?.mastery ?? []) as ScopedMastery[];
    const weak = [...mastery].filter((item) => item.p_mastery < 0.6).sort((a, b) => a.p_mastery - b.p_mastery)[0] ?? null;
    return {
      tasks,
      nextTask,
      currentStage,
      stageTasks,
      stageDone,
      stagePct,
      topic,
      tutorHref,
      quizHref,
      todos: today.todos,
      totalDue: data.reviews?.total_due ?? data.reviews?.items?.length ?? 0,
      weak,
    };
  }, [data]);

  const toggleTask = async (task: FlatPlanTask) => {
    if (!data?.plan || !task.task_key || togglingKey) return;
    const snapshot = data;
    setTogglingKey(task.task_key);
    setToggleError(null);
    setData((current) => current ? {
      ...current,
      plan: current.plan ? {
        ...current.plan,
        progress: { ...current.plan.progress, done: current.plan.progress.done + (task.done ? -1 : 1), ratio: current.plan.progress.total ? (current.plan.progress.done + (task.done ? -1 : 1)) / current.plan.progress.total : 0 },
        structure: { stages: current.plan.structure.stages.map((stage) => ({ ...stage, days: stage.days.map((day) => ({ ...day, tasks: day.tasks.map((item) => item.task_key === task.task_key ? { ...item, done: !task.done } : item) })) })) },
      } : null,
    } : current);
    try {
      const result = await apiJson<PlanToggleResult>(`/api/plans/${data.plan.id}/tasks/${task.task_key}/toggle`, { method: "POST", body: { done: !task.done } });
      setData((current) => {
        if (!current?.plan) return current;
        return {
          ...current,
          plan: {
            ...current.plan,
            progress: result.progress,
            structure: {
              stages: current.plan.structure.stages.map((stage) => ({
                ...stage,
                days: stage.days.map((day) => ({
                  ...day,
                  tasks: day.tasks.map((item) => item.task_key === task.task_key ? { ...item, done: result.done } : item),
                })),
              })),
            },
          },
        };
      });
    } catch {
      setData(snapshot);
      setToggleError("任务状态更新失败，已恢复原状态");
    } finally {
      setTogglingKey(null);
    }
  };

  if (!data || !view) return <DashboardSkeleton />;
  if (!data.plan && view.todos.length === 0) {
    return <div className="mx-auto w-full max-w-[1480px] px-5 py-7 sm:px-8 lg:px-12"><EmptyToday /></div>;
  }

  const planPct = Math.round((data.plan?.progress.ratio ?? 0) * 100);
  const planDone = data.plan?.progress.done ?? 0;
  const planTotal = data.plan?.progress.total ?? 0;
  const doneTodos = view.todos.filter((item) => item.done).length;

  return (
    <div className="mx-auto w-full max-w-[1480px] space-y-5 px-5 py-7 sm:px-8 lg:px-12 lg:py-8">
      <Panel className="p-6 sm:p-7">
        <div className="flex items-stretch justify-between gap-8 lg:gap-14">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2 text-[11px] font-medium text-ink-secondary">
              <span>继续上次学习</span><span className="h-1 w-1 rounded-full bg-ink-muted" /><span className="font-normal text-ink-muted">按当前计划继续</span>
            </div>
            <h2 className="mt-4 text-[21px] font-semibold tracking-[-0.025em] text-ink">{view.currentStage}</h2>
            <div className="mt-1 text-[13px] leading-6 text-ink-secondary"><span>当前任务：</span><MathText text={view.nextTask?.title ?? data.plan?.goal ?? "完成今天的学习安排"} /></div>

            <div className="mt-6 flex items-center gap-3">
              <div className="h-[5px] flex-1 overflow-hidden rounded-full bg-[#eceae6]"><span className="block h-full rounded-full bg-indigo-600" style={{ width: `${view.stagePct}%` }} /></div>
              <span className="shrink-0 text-[12px] text-ink-secondary"><strong className="font-semibold text-indigo-600">本章 {view.stagePct}%</strong>{view.nextTask ? ` · 当前任务约 ${view.nextTask.minutes} 分钟` : ""}</span>
            </div>

            <div className="mt-6 flex flex-wrap gap-2.5">
              <Link href={view.tutorHref} className="inline-flex h-10 items-center gap-2 rounded-[8px] bg-ink px-5 text-[13px] font-semibold text-white transition-colors hover:bg-black">进入苏格拉底辅导学习<ArrowIcon /></Link>
              <Link href={view.quizHref} className="inline-flex h-10 items-center rounded-[8px] border border-line-strong bg-white px-4 text-[13px] text-ink-secondary transition-colors hover:border-ink-muted hover:text-ink">改做练习题</Link>
            </div>
            <p className="mt-4 text-[12px] text-ink-muted">苏问是你的 AI 苏格拉底——不急着给答案，先陪你想明白。</p>
          </div>

          <div className="hidden w-[clamp(220px,24%,300px)] shrink-0 border-l border-line pl-7 lg:flex lg:flex-col lg:justify-between">
            <div>
              <p className="text-[11px] font-semibold tracking-[0.06em] text-ink-muted">当前阶段进度</p>
              <ol className="mt-4 space-y-3">
                {view.stageTasks.map((task, index) => (
                  <li key={task.task_key ?? index} className="flex items-center gap-2.5 text-[12px]">
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${task.done ? "bg-success-600" : task.task_key === view.nextTask?.task_key ? "bg-ink" : "bg-line-strong"}`} />
                    <span className={`min-w-0 flex-1 truncate ${task.done ? "text-success-600" : task.task_key === view.nextTask?.task_key ? "font-medium text-ink" : "text-ink-muted"}`}><MathText text={task.title} singleLine /></span>
                    {task.done && <span className="text-success-600">✓</span>}
                  </li>
                ))}
              </ol>
              {view.stageTasks.length === 0 && <p className="text-[11px] leading-5 text-ink-muted">计划任务生成后，会在这里显示当前阶段与完成状态。</p>}
            </div>
            <div className="mt-5 flex items-center justify-between border-t border-line pt-4 text-[11px] text-ink-muted"><span>计划总进度</span><strong className="font-semibold text-indigo-600">{planTotal > 0 ? `${planPct}% · ${planDone}/${planTotal}` : "等待生成任务"}</strong></div>
          </div>
        </div>
      </Panel>

      {toggleError && <p role="alert" className="rounded-[8px] border border-danger-200 bg-danger-50 px-4 py-2 text-[11px] text-danger-700">{toggleError}</p>}

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <Panel>
          <button type="button" onClick={() => setTodosExpanded((value) => !value)} className={`flex w-full items-center justify-between px-5 py-4 text-left ${todosExpanded ? "border-b border-line" : ""}`}>
            <span className="flex items-center gap-2.5"><strong className="text-[14px] font-semibold text-ink">今日待办</strong><span className="rounded-full bg-[#f3f2ef] px-2 py-0.5 text-[11px] text-ink-secondary">{doneTodos} / {view.todos.length} 完成</span>{view.totalDue > 0 && <span className="text-[10px] text-warning-600">{view.totalDue} 项今日到期</span>}</span>
            <span className="text-ink-muted"><ChevronIcon open={todosExpanded} /></span>
          </button>

          {todosExpanded && (
            <>
              {view.todos.length > 0 ? <ul className="divide-y divide-line">
                {view.todos.map((item) => (
                  <li key={item.id} className="group flex min-h-[66px] items-center gap-3 px-5 py-3 transition-colors hover:bg-[#fafaf9]">
                    {item.task?.task_key ? (
                      <button type="button" disabled={togglingKey === item.task.task_key} onClick={() => void toggleTask(item.task!)} aria-label={item.done ? `取消完成 ${item.title}` : `完成 ${item.title}`} className={`flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-[4px] border text-[10px] ${item.done ? "border-success-600 bg-success-600 text-white" : "border-line-strong bg-white text-transparent hover:border-ink-muted"}`}>✓</button>
                    ) : <Link href={item.href} aria-label={`开始 ${item.title}`} className="h-[18px] w-[18px] shrink-0 rounded-[4px] border border-line-strong bg-white transition-colors hover:border-ink-muted" />}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        {item.task ? <button type="button" onClick={() => setOpenTask({ task: item.task!, stageName: item.task!.stage, day: item.task!.day, planKbId: data.plan?.kb_id })} className={`min-w-0 truncate text-left text-[13px] font-medium hover:text-brand-700 ${item.done ? "text-ink-muted line-through" : "text-ink"}`}><MathText text={item.title} singleLine /></button> : <p className={`min-w-0 truncate text-[13px] font-medium ${item.done ? "text-ink-muted line-through" : "text-ink"}`}><MathText text={item.title} singleLine /></p>}
                        {item.dueLabel && !item.done && <span className="shrink-0 rounded-full bg-warning-50 px-2 py-0.5 text-[10px] font-semibold text-warning-700">{item.dueLabel}</span>}
                      </div>
                      <div className="mt-1 flex items-center gap-2 text-[10px] text-ink-muted"><span className="rounded-[3px] bg-[#f1f0ed] px-1.5 py-0.5 font-semibold text-ink-secondary">{item.type === "review" ? "复习" : item.type === "practice" ? "练习" : "学习"}</span><span className="truncate">{item.context}</span></div>
                      {item.task && <p className="mt-1 truncate text-[10px] leading-4 text-ink-muted"><span>目标：</span><MathText text={item.task.objective || item.task.completion_criteria || "完成本任务并检查理解"} singleLine /></p>}
                    </div>
                    <span className="shrink-0 text-[11px] text-ink-muted">{item.duration}</span>
                    {!item.done && <Link href={item.href} className="inline-flex shrink-0 rounded-[7px] px-2 py-1 text-[11px] font-medium text-indigo-600 hover:bg-indigo-50">开始任务 →</Link>}
                  </li>
                ))}
              </ul> : <p className="px-5 py-10 text-center text-[12px] text-ink-muted">今天没有待办，保持当前节奏即可。</p>}
              {data.plan && <div className="flex items-center justify-between border-t border-line px-5 py-3 text-[11px] text-ink-muted"><span>来自「{data.plan.goal}」· 最近创建的计划用于今日任务与自动调整</span><Link href={`/plans?id=${data.plan.id}`} className="font-medium text-indigo-600 hover:underline">查看完整计划 →</Link></div>}
            </>
          )}
        </Panel>

        <Panel>
          <header className="border-b border-line px-5 py-4"><h2 className="text-[14px] font-semibold text-ink">重点提醒</h2></header>
          {view.weak ? (
            <div>
              <button type="button" onClick={() => setReminderExpanded((value) => !value)} className="flex w-full items-center gap-3 px-5 py-4 text-left transition-colors hover:bg-[#fafaf9]">
                <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-warning-600" />
                <span className="min-w-0 flex-1"><strong className="block truncate text-[13px] font-medium text-ink">{view.weak.name}</strong><span className="mt-1 block text-[11px] text-ink-muted">{view.weak.subject || view.weak.kb_name || "知识点"} · 当前掌握 {Math.round(view.weak.p_mastery * 100)}%</span></span>
                <span className="text-ink-muted"><ChevronIcon open={reminderExpanded} /></span>
              </button>
              {reminderExpanded && <div className="px-5 pb-5 pl-9">
                <p className="rounded-[7px] border-l-2 border-line-strong bg-[#f5f4f2] px-3 py-2.5 text-[12px] leading-5 text-ink-secondary">这个知识点是当前掌握度最低的内容。建议先通过提问梳理关键概念，再用 3 道定向题验证。</p>
                <div className="mt-3 flex flex-wrap gap-4 text-[12px] font-medium"><Link href={`/tutor?problem=${encodeURIComponent(`请引导我理解：${view.weak.name}`)}`} className="text-indigo-600 hover:underline">带着这个知识点进入辅导 →</Link><Link href={`/quiz?kc=${encodeURIComponent(view.weak.name)}&n=3`} className="text-ink-secondary hover:text-ink">改做相关练习</Link></div>
              </div>}
            </div>
          ) : <p className="px-5 py-8 text-center text-[12px] text-ink-muted">暂未发现需要优先加强的知识点。</p>}
        </Panel>
      </div>
      {openTask && <PlanTaskDrawer location={openTask} onClose={() => setOpenTask(null)} />}
    </div>
  );
}
