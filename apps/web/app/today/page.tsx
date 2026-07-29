"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { useAuth } from "@/lib/auth-context";
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

interface NextActionItem {
  type: string;
  title: string;
  reason: string;
  href: string;
}

interface NextActionsResponse {
  items: NextActionItem[];
}

type ScopedMastery = MasteryItem & { kb_name?: string | null; subject?: string | null };

interface DashboardData {
  loadedAt: number;
  actions: NextActionItem[];
  plans: PlanListItem[];
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

function SectionCard({ title, extra, children, className = "" }: { title: ReactNode; extra?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`overflow-hidden rounded-[14px] border border-line bg-white ${className}`}>
      <header className="flex min-h-11 items-center justify-between gap-3 border-b border-line px-4 py-2.5">
        <h2 className="text-[13px] font-semibold text-ink">{title}</h2>
        {extra}
      </header>
      {children}
    </section>
  );
}

function EmptyRow({ children }: { children: ReactNode }) {
  return <div className="px-4 py-8 text-center text-[12px] text-ink-muted">{children}</div>;
}

function DashboardSkeleton() {
  return (
    <div className="mx-auto w-full max-w-[1240px] animate-pulse space-y-4 px-5 py-5 sm:px-7 lg:px-8">
      <div className="h-48 rounded-[14px] bg-white" />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_280px]">
        <div className="space-y-4"><div className="h-64 rounded-[14px] bg-white" /><div className="h-40 rounded-[14px] bg-white" /></div>
        <div className="space-y-4"><div className="h-64 rounded-[14px] bg-white" /><div className="h-40 rounded-[14px] bg-white" /></div>
      </div>
    </div>
  );
}

export default function TodayPage() {
  const { user } = useAuth();
  const [data, setData] = useState<DashboardData | null>(null);
  const [togglingKey, setTogglingKey] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      const [actionsResult, plansResult, reviewsResult, profileResult] = await Promise.allSettled([
        apiJson<NextActionsResponse>("/api/next-actions"),
        apiJson<PlanListItem[]>("/api/plans"),
        apiJson<ReviewTodayResponse>("/api/reviews/today"),
        apiJson<ProfileResponse>("/api/profile"),
      ]);
      if (!alive) return;
      const plans = plansResult.status === "fulfilled" ? plansResult.value : [];
      const latest = [...plans].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0];
      let plan: PlanDetail | null = null;
      if (latest) {
        try { plan = await apiJson<PlanDetail>(`/api/plans/${latest.id}`); } catch { plan = null; }
      }
      if (!alive) return;
      setData({
        loadedAt: Date.now(),
        actions: actionsResult.status === "fulfilled" ? actionsResult.value.items : [],
        plans,
        plan,
        reviews: reviewsResult.status === "fulfilled" ? reviewsResult.value : null,
        profile: profileResult.status === "fulfilled" ? profileResult.value : null,
      });
    })();
    return () => { alive = false; };
  }, []);

  const view = useMemo(() => {
    if (!data) return null;
    const mastery = (data.profile?.mastery ?? []) as ScopedMastery[];
    const tasks = (data.plan?.structure.stages ?? []).flatMap((stage) =>
      stage.days.flatMap((day) => day.tasks.map((task) => ({ ...task, day: day.day, stage: stage.name }))),
    );
    const nextTask = tasks.find((task) => !task.done);
    const continueAction = data.actions[0] ?? (nextTask ? {
      type: "plan",
      title: nextTask.title,
      reason: `${nextTask.stage} · 第 ${nextTask.day} 天 · ${nextTask.minutes} 分钟`,
      href: "/plans",
    } : null);
    const weak = [...mastery].filter((item) => item.p_mastery < 0.6).sort((a, b) => a.p_mastery - b.p_mastery).slice(0, 4);
    const stageTasks = nextTask ? tasks.filter((task) => task.stage === nextTask.stage).slice(0, 4) : tasks.slice(0, 4);
    return {
      tasks: tasks.slice(0, 4),
      stageTasks,
      continueAction,
      weak,
      reviews: (data.reviews?.items ?? []).slice(0, 4),
      totalDue: data.reviews?.total_due ?? data.reviews?.items.length ?? 0,
      changes: (data.profile?.recent_changes ?? []).slice(0, 4),
      progress: data.plan?.progress ?? null,
      planGoal: data.plan?.goal ?? null,
    };
  }, [data]);

  const toggleTask = async (taskKey: string, done: boolean) => {
    if (!data?.plan || togglingKey) return;
    setTogglingKey(taskKey);
    try {
      const result = await apiJson<PlanToggleResult>(`/api/plans/${data.plan.id}/tasks/${taskKey}/toggle`, { method: "POST", body: { done } });
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
                  tasks: day.tasks.map((task) => task.task_key === taskKey ? { ...task, done: result.done } : task),
                })),
              })),
            },
          },
        };
      });
    } finally {
      setTogglingKey(null);
    }
  };

  if (!data || !view) return <DashboardSkeleton />;

  const displayName = user?.email?.split("@")[0] || "同学";
  const progressPct = Math.round((view.progress?.ratio ?? 0) * 100);

  return (
    <div className="mx-auto w-full max-w-[1240px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      <section className="relative overflow-hidden rounded-[14px] border border-line bg-white">
        <span className="absolute inset-y-0 left-0 w-[3px] bg-brand-600" aria-hidden />
        <div className="grid gap-6 p-5 pl-6 lg:grid-cols-[minmax(0,1fr)_250px] lg:p-6 lg:pl-7">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2 text-[10px]">
              <span className="rounded-[5px] bg-brand-50 px-2 py-1 font-medium text-brand-700">继续上次</span>
              <span className="text-ink-muted">{view.progress ? `${view.progress.done}/${view.progress.total} 项已完成` : `欢迎，${displayName}`}</span>
            </div>
            <h2 className="mt-3 text-[18px] font-semibold tracking-[-0.02em] text-ink">{view.continueAction?.title ?? "开始建立你的第一条学习路径"}</h2>
            <p className="mt-1 text-[12px] leading-5 text-ink-secondary">{view.continueAction?.reason ?? "完成一次对话、练习或创建学习计划后，这里会显示最值得继续的内容。"}</p>
            <div className="mt-5 flex items-center gap-3">
              <div className="h-1 flex-1 overflow-hidden rounded-full bg-[#ebeae7]"><span className="block h-full rounded-full bg-[#818cf8]" style={{ width: `${progressPct}%` }} /></div>
              <span className="shrink-0 text-[11px] text-ink-muted">{view.progress ? `${progressPct}%` : "尚未开始"}</span>
            </div>
            <div className="mt-5 flex flex-wrap gap-2">
              <Link href={view.continueAction?.href ?? "/chat?new=1"} className="inline-flex h-9 items-center gap-2 rounded-[8px] bg-brand-600 px-4 text-[12px] font-medium text-white hover:bg-brand-800">{view.continueAction ? "继续学习" : "开始学习"}<ArrowIcon /></Link>
              <Link href="/quiz?weak=1" className="inline-flex h-9 items-center rounded-[8px] border border-line-strong bg-white px-4 text-[12px] font-medium text-ink-secondary hover:border-brand-200 hover:text-brand-700">做薄弱练习</Link>
            </div>
          </div>
          <div className="rounded-[11px] border border-line bg-[#fafaf9] p-4">
            <p className="text-[10px] font-medium text-ink-muted">{view.planGoal ? "当前计划进度" : "下一步"}</p>
            {view.stageTasks.length > 0 ? (
              <ol className="mt-3 space-y-2.5">
                {view.stageTasks.map((task, index) => (
                  <li key={task.task_key ?? `${task.day}-${index}`} className={`flex items-center gap-2 text-[11px] ${task.done ? "text-success-600" : index === 0 ? "font-medium text-brand-700" : "text-ink-muted"}`}>
                    <span className={`h-1.5 w-1.5 rounded-full ${task.done ? "bg-success-500" : index === 0 ? "bg-brand-600" : "bg-line-strong"}`} />
                    <span className="min-w-0 flex-1 truncate">{task.title}</span>
                    {task.done && <span aria-label="已完成">✓</span>}
                  </li>
                ))}
              </ol>
            ) : <p className="mt-3 text-[11px] leading-5 text-ink-muted">创建学习计划后会在这里显示章节与任务进度。</p>}
            {view.progress && <div className="mt-4 flex justify-between border-t border-line pt-3 text-[10px] text-ink-muted"><span>已完成</span><strong className="font-semibold text-brand-700">{view.progress.done} / {view.progress.total} 项</strong></div>}
          </div>
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_280px] lg:items-start">
        <div className="space-y-4">
          <SectionCard title={<>今日任务 <span className="ml-1 font-normal text-ink-muted">{view.progress ? `${view.progress.done}/${view.progress.total} 完成` : ""}</span></>} extra={<Link href="/plans" className="text-[11px] font-medium text-brand-700 hover:underline">查看完整计划 →</Link>}>
            {view.tasks.length > 0 ? (
              <ul className="divide-y divide-line">
                {view.tasks.map((task, index) => (
                  <li key={task.task_key ?? `${task.day}-${index}`} className="flex min-h-[52px] items-center gap-3 px-4 py-2.5">
                    <button type="button" disabled={!task.task_key || togglingKey === task.task_key} onClick={() => task.task_key && void toggleTask(task.task_key, !task.done)} aria-label={task.done ? `取消完成 ${task.title}` : `完成 ${task.title}`} className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-[4px] border text-[10px] ${task.done ? "border-success-500 bg-success-500 text-white" : "border-line-strong bg-white text-transparent hover:border-brand-400"}`}>✓</button>
                    <div className="min-w-0 flex-1">
                      <p className={`truncate text-[12px] font-medium ${task.done ? "text-ink-muted line-through" : "text-ink"}`}>{task.title}</p>
                      <p className="mt-0.5 truncate text-[10px] text-ink-muted">{task.stage} · 第 {task.day} 天</p>
                    </div>
                    <span className="text-[10px] text-ink-muted">{task.minutes} 分钟</span>
                  </li>
                ))}
              </ul>
            ) : <EmptyRow>还没有今日任务。先创建一个学习计划。</EmptyRow>}
          </SectionCard>

          <SectionCard title="需要加强" extra={<Link href="/errors" className="text-[11px] text-ink-muted hover:text-brand-700">查看错题本 →</Link>}>
            {view.weak.length > 0 ? (
              <ul className="divide-y divide-line">
                {view.weak.map((item) => (
                  <li key={item.kc_id} className="flex min-h-[50px] items-center gap-3 px-4 py-3">
                    <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-warning-500" />
                    <div className="min-w-0 flex-1"><p className="truncate text-[12px] font-medium text-ink">{item.name}</p><p className="mt-0.5 text-[10px] text-ink-muted">{item.subject || item.kb_name || "知识点"} · 当前掌握 {Math.round(item.p_mastery * 100)}%</p></div>
                    <Link href={`/quiz?kc=${encodeURIComponent(item.name)}&n=3`} className="text-[11px] text-brand-700 hover:underline">练一下</Link>
                  </li>
                ))}
              </ul>
            ) : <EmptyRow>暂未发现需要加强的知识点。完成练习后会持续更新。</EmptyRow>}
          </SectionCard>
        </div>

        <aside className="space-y-4">
          <SectionCard title={<>待复习 <span className="ml-1 rounded-full bg-warning-50 px-1.5 py-0.5 text-[10px] font-medium text-warning-600">今日 {view.totalDue} 项</span></>} extra={<span className="text-[10px] text-ink-muted">队列 {(data.reviews?.items ?? []).length} 项</span>}>
            {view.reviews.length > 0 ? (
              <ul className="divide-y divide-line">
                {view.reviews.map((item) => (
                  <li key={item.error_item_id} className="px-4 py-3">
                    <div className="flex gap-2"><span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-warning-500" /><p className="line-clamp-2 text-[11px] font-medium leading-4 text-ink"><MathText text={item.question.stem} /></p></div>
                    <p className="ml-3.5 mt-1 text-[10px] text-warning-600">今日到期</p>
                  </li>
                ))}
              </ul>
            ) : <EmptyRow>今天没有到期复习，保持当前节奏即可。</EmptyRow>}
            {view.totalDue > 0 && <div className="flex items-center justify-between border-t border-line px-4 py-2.5"><Link href="/reviews" className="text-[10px] text-ink-muted hover:text-brand-700">查看全部 {view.totalDue} 项 ↓</Link><Link href="/reviews" className="rounded-[6px] bg-brand-600 px-3 py-1.5 text-[11px] font-medium text-white">开始复习</Link></div>}
          </SectionCard>

          <SectionCard title="最近学习">
            {view.changes.length > 0 ? (
              <ul className="divide-y divide-line">
                {view.changes.map((change) => (
                  <li key={change.id} className="px-4 py-3"><div className="flex items-center justify-between gap-2"><p className="truncate text-[11px] font-medium text-ink">{change.kc_name}</p><span className="text-[10px] text-ink-muted">{Math.round(change.new_p * 100)}%</span></div><p className="mt-1 text-[10px] text-ink-muted">掌握度 {Math.round(change.old_p * 100)}% → {Math.round(change.new_p * 100)}%</p><Link href="/profile" className="mt-2 inline-flex rounded-[5px] bg-brand-50 px-2 py-1 text-[10px] font-medium text-brand-700">查看画像 →</Link></li>
                ))}
              </ul>
            ) : <EmptyRow>还没有学习记录。完成第一次学习后会显示在这里。</EmptyRow>}
          </SectionCard>
        </aside>
      </div>
    </div>
  );
}
