"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { useAuth } from "@/lib/auth-context";
import { apiJson } from "@/lib/api";
import type {
  MasteryItem,
  PlanDetail,
  PlanListItem,
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

type ScopedMastery = MasteryItem & {
  kb_name?: string | null;
  subject?: string | null;
};

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

function OverviewItem({ label, value, note, href }: { label: string; value: string; note: string; href: string }) {
  return (
    <Link href={href} className="group min-w-0 px-5 py-5 first:pl-0 max-md:[&:nth-child(odd)]:pl-0 md:border-l md:border-line md:first:border-l-0">
      <p className="text-[11px] font-medium text-ink-muted">{label}</p>
      <div className="mt-2 flex items-end gap-2">
        <strong className="text-[26px] font-semibold leading-none tracking-[-0.03em] text-ink">{value}</strong>
        <span className="mb-0.5 truncate text-[11px] text-ink-muted">{note}</span>
      </div>
      <span className="mt-3 block h-[2px] w-5 bg-line-strong transition-all group-hover:w-10 group-hover:bg-brand-600" />
    </Link>
  );
}

function SectionTitle({ title, action }: { title: string; action?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-line pb-3">
      <h2 className="text-sm font-semibold text-ink">{title}</h2>
      {action}
    </div>
  );
}

function DashboardSkeleton() {
  return (
    <div className="mx-auto max-w-[1180px] animate-pulse px-5 py-8 sm:px-8 lg:px-10">
      <div className="h-8 w-52 rounded bg-line" />
      <div className="mt-9 grid grid-cols-2 border-y border-line md:grid-cols-4">
        {[0, 1, 2, 3].map((item) => <div key={item} className="h-28 border-line p-5 md:border-l first:border-l-0"><div className="h-3 w-16 rounded bg-line" /><div className="mt-4 h-7 w-24 rounded bg-line" /></div>)}
      </div>
      <div className="mt-9 grid gap-10 lg:grid-cols-[minmax(0,1fr)_310px]">
        <div className="h-72 rounded-overlay bg-line/70" />
        <div className="h-72 rounded-overlay bg-line/70" />
      </div>
    </div>
  );
}

export default function TodayPage() {
  const { user } = useAuth();
  const [data, setData] = useState<DashboardData | null>(null);

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
        try {
          plan = await apiJson<PlanDetail>(`/api/plans/${latest.id}`);
        } catch {
          plan = null;
        }
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
    return () => {
      alive = false;
    };
  }, []);

  const view = useMemo(() => {
    if (!data) return null;
    const mastery = (data.profile?.mastery ?? []) as ScopedMastery[];
    const weekStart = data.loadedAt - 7 * 24 * 60 * 60 * 1000;
    const weekEvidence = (data.profile?.recent_changes ?? []).filter((item) => Date.parse(item.at) >= weekStart).length;
    const weakCount = mastery.filter((item) => item.p_mastery < 0.6).length;
    const totalDue = data.reviews?.total_due ?? data.reviews?.items.length ?? 0;
    const progress = data.plan?.progress;

    const planTasks = (data.plan?.structure.stages ?? []).flatMap((stage) =>
      stage.days.flatMap((day) => day.tasks.map((task) => ({ ...task, day: day.day, stage: stage.name }))),
    );
    const nextTask = planTasks.find((task) => !task.done);
    const continueAction = data.actions[0] ?? (nextTask ? {
      type: "plan",
      title: nextTask.title,
      reason: `${nextTask.stage} · 第 ${nextTask.day} 天 · ${nextTask.minutes} 分钟`,
      href: "/plans",
    } : null);

    const subjects = new Map<string, { total: number; count: number; weak: number }>();
    for (const item of mastery) {
      const name = item.subject || item.kb_name || "全部知识点";
      const current = subjects.get(name) ?? { total: 0, count: 0, weak: 0 };
      current.total += item.p_mastery;
      current.count += 1;
      if (item.p_mastery < 0.6) current.weak += 1;
      subjects.set(name, current);
    }

    return {
      weekEvidence,
      weakCount,
      totalDue,
      progress,
      continueAction,
      tasks: planTasks.filter((task) => !task.done).slice(0, 4),
      subjects: [...subjects.entries()].map(([name, item]) => ({ name, mastery: item.count ? item.total / item.count : 0, weak: item.weak })).slice(0, 4),
      changes: (data.profile?.recent_changes ?? []).slice(0, 4),
    };
  }, [data]);

  if (!data || !view) return <DashboardSkeleton />;

  const date = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "long", day: "numeric", weekday: "long" }).format(new Date(data.loadedAt));
  const userName = user?.email?.split("@")[0] || "同学";

  return (
    <div className="mx-auto w-full max-w-[1180px] px-5 py-8 sm:px-8 lg:px-10 lg:py-10">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-[11px] font-medium text-ink-muted">{date}</p>
          <h1 className="mt-2 text-[28px] font-semibold leading-tight tracking-[-0.04em] text-ink">上午好，{userName}</h1>
          <p className="mt-2 text-sm text-ink-secondary">把注意力放在下一件最值得做的事上。</p>
        </div>
        <Link href="/chat?new=1" className="inline-flex h-10 items-center gap-2 rounded-control bg-ink px-4 text-[13px] font-medium text-white transition-colors hover:bg-black">
          开始学习 <ArrowIcon />
        </Link>
      </header>

      <section className="mt-9 grid grid-cols-2 border-y border-line md:grid-cols-4">
        <OverviewItem label="本周学习证据" value={`${view.weekEvidence}`} note="次有效更新" href="/profile" />
        <OverviewItem label="当前计划" value={view.progress ? `${Math.round(view.progress.ratio * 100)}%` : "—"} note={view.progress ? `${view.progress.done}/${view.progress.total} 项` : "尚未建立"} href="/plans" />
        <OverviewItem label="到期复习" value={`${view.totalDue}`} note="题待处理" href="/reviews" />
        <OverviewItem label="待巩固" value={`${view.weakCount}`} note="个知识点" href="/quiz?weak=1" />
      </section>

      <div className="mt-10 grid gap-12 lg:grid-cols-[minmax(0,1fr)_310px]">
        <div className="min-w-0 space-y-11">
          <section>
            <SectionTitle title="继续学习" action={<Link href="/chat" className="text-[11px] font-medium text-brand-600 hover:text-brand-700">进入学习工作台</Link>} />
            {view.continueAction ? (
              <Link href={view.continueAction.href} className="group mt-5 flex items-center justify-between gap-6 rounded-overlay border border-line bg-surface px-5 py-5 transition-colors hover:border-line-strong">
                <div className="min-w-0">
                  <p className="text-[10px] font-semibold tracking-[0.08em] text-brand-600">系统建议</p>
                  <h3 className="mt-2 truncate text-lg font-semibold text-ink">{view.continueAction.title}</h3>
                  <p className="mt-1 line-clamp-2 text-xs leading-5 text-ink-secondary">{view.continueAction.reason}</p>
                </div>
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-ink text-white transition-transform group-hover:translate-x-1"><ArrowIcon /></span>
              </Link>
            ) : (
              <div className="mt-5 border-b border-line py-8 text-sm text-ink-muted">还没有待继续的学习内容。可以从一次新对话开始。</div>
            )}
          </section>

          <section>
            <SectionTitle title="后续任务" action={<Link href="/plans" className="text-[11px] text-ink-muted hover:text-ink">查看计划</Link>} />
            <div className="divide-y divide-line">
              {view.tasks.length > 0 ? view.tasks.map((task, index) => (
                <Link href="/plans" key={`${task.day}-${index}-${task.title}`} className="group flex items-center gap-4 py-4">
                  <span className="w-12 shrink-0 text-[11px] font-medium text-ink-muted">第 {task.day} 天</span>
                  <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink group-hover:text-brand-600">{task.title}</span>
                  <span className="shrink-0 text-[11px] text-ink-muted">{task.minutes} 分钟</span>
                  <ArrowIcon />
                </Link>
              )) : <p className="py-7 text-sm text-ink-muted">暂无计划任务。</p>}
            </div>
          </section>
        </div>

        <aside className="space-y-10">
          <section>
            <SectionTitle title="科目概览" action={<Link href="/reports" className="text-[11px] text-ink-muted hover:text-ink">完整报告</Link>} />
            <div className="divide-y divide-line">
              {view.subjects.length > 0 ? view.subjects.map((subject) => (
                <Link href="/profile" key={subject.name} className="block py-4">
                  <div className="flex items-center justify-between gap-3 text-xs">
                    <span className="truncate font-medium text-ink">{subject.name}</span>
                    <span className="text-ink-secondary">{Math.round(subject.mastery * 100)}%</span>
                  </div>
                  <div className="mt-2 h-1 overflow-hidden rounded-full bg-line"><span className="block h-full rounded-full bg-brand-600" style={{ width: `${Math.round(subject.mastery * 100)}%` }} /></div>
                  <p className="mt-2 text-[10px] text-ink-muted">{subject.weak ? `${subject.weak} 个待巩固` : "当前状态稳定"}</p>
                </Link>
              )) : <p className="py-6 text-xs text-ink-muted">完成练习后，这里会出现科目概览。</p>}
            </div>
          </section>

          <section>
            <SectionTitle title="最近活动" />
            <ol className="mt-4 space-y-4">
              {view.changes.length > 0 ? view.changes.map((change) => (
                <li key={change.id} className="grid grid-cols-[8px_minmax(0,1fr)] gap-3">
                  <span className="mt-1.5 h-2 w-2 rounded-full bg-brand-600" />
                  <div>
                    <p className="truncate text-xs font-medium text-ink">{change.kc_name}</p>
                    <p className="mt-1 text-[10px] text-ink-muted">掌握度 {Math.round(change.old_p * 100)}% → {Math.round(change.new_p * 100)}%</p>
                  </div>
                </li>
              )) : <li className="text-xs text-ink-muted">还没有学习活动。</li>}
            </ol>
          </section>
        </aside>
      </div>
    </div>
  );
}
