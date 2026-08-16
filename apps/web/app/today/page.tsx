"use client";

// 今日（UI 重构阶段 4 加厚）：主推卡（next-actions 唯一决策源，保留）+ 今日计划任务卡
// （复用 PlanTodayCard，无活跃计划整块隐藏）+ 到期复习计数条（0=复习清零）+
// 本周活跃摘要一行（/api/reports/learning 已有聚合，拿不到不渲染）。
// 次要推荐 <details> 与页脚「真实学习证据」说明保留。

import Link from "next/link";
import { useEffect, useState } from "react";

import { ErrorBanner } from "@/components/common";
import { TodayPlanSection } from "@/components/plans/today-plan-section";
import { apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type {
  LearningReportResponse,
  NextActionItem,
  NextActionsResponse,
  ReviewTodayResponse,
} from "@/lib/types";

type TodayState =
  | { status: "loading" }
  | { status: "ready"; items: NextActionItem[] }
  | { status: "empty" }
  | { status: "error"; message: string };

const ACTION_LABELS: Record<string, string> = {
  review: "到期复习",
  resume_insight: "继续卡点",
  weak_practice: "薄弱点练习",
  plan_task: "计划任务",
  cold_start: "建立起点",
};

/** 阶段 3（规则 B）：复习/练习类行动改入专注会话；后端 href 契约不变，前端做映射。 */
function focusActionHref(href: string): string {
  const from = encodeURIComponent("/today");
  if (href === "/reviews") {
    return `/practice/session?mode=review&from=${from}`;
  }
  if (href === "/quiz?weak=1") {
    return `/practice/session?mode=quiz&from=${from}&weak=1`;
  }
  if (href === "/quiz") {
    return `/practice/session?mode=quiz&from=${from}`;
  }
  return href;
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-4 w-4" aria-hidden>
      <path d="M4 10h12m-4-4 4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** 到期复习计数条：今日到期 N 题 → 专注复习（from=/today）；0 题=复习清零。 */
function ReviewCountStrip() {
  const [dueCount, setDueCount] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    void apiJson<ReviewTodayResponse>("/api/reviews/today")
      .then((data) => {
        if (alive) setDueCount(Math.max(data.total_due ?? data.items.length, data.items.length));
      })
      .catch(() => {
        if (alive) setDueCount(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (dueCount === null) return null;
  if (dueCount === 0) {
    return (
      <p className="flex items-center gap-2 text-body-sm text-ink-muted">
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-success-50 text-caption font-bold text-success-600">✓</span>
        复习清零：今天没有到期错题。
      </p>
    );
  }
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-[12px] border border-line bg-white px-4 py-3">
      <p className="text-body-sm text-ink-secondary">
        <strong className="text-ink">今日到期 {dueCount} 题</strong>
        <span className="ml-2 text-caption text-ink-muted">按遗忘曲线安排，答对两次即攻克</span>
      </p>
      <Link
        href="/practice/session?mode=review&from=%2Ftoday"
        className="inline-flex h-9 items-center gap-1.5 rounded-[8px] bg-ink px-4 text-[13px] font-semibold text-white transition-colors hover:bg-black"
      >
        开始复习
        <ArrowIcon />
      </Link>
    </div>
  );
}

/** 本周活跃摘要（一行小字）：复用 /api/reports/learning 已有聚合；拿不到整行不渲染。 */
function WeeklySummary() {
  const [summary, setSummary] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void apiJson<LearningReportResponse>("/api/reports/learning")
      .then((report) => {
        if (!alive) return;
        const metrics = report.metrics;
        if (!metrics) {
          setSummary(null);
          return;
        }
        const delta =
          metrics.attempt_count_delta > 0
            ? `（较前 7 天 +${metrics.attempt_count_delta}）`
            : metrics.attempt_count_delta < 0
              ? `（较前 7 天 ${metrics.attempt_count_delta}）`
              : "";
        const accuracy =
          metrics.accuracy != null ? ` · 正确率 ${Math.round(metrics.accuracy * 100)}%` : "";
        setSummary(
          `近 7 天作答 ${metrics.attempt_count} 次${delta} · 活跃 ${metrics.active_days}/30 天${accuracy}`,
        );
      })
      .catch(() => {
        if (alive) setSummary(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!summary) return null;
  return <p className="text-center text-caption text-ink-muted">{summary}</p>;
}

export default function TodayPage() {
  const [reloadKey, setReloadKey] = useState(0);
  const [state, setState] = useState<TodayState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    void apiJson<NextActionsResponse>("/api/next-actions")
      .then((data) => {
        if (!active) return;
        setState(data.items.length > 0 ? { status: "ready", items: data.items } : { status: "empty" });
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setState({
          status: "error",
          message: reason instanceof Error ? reason.message : "下一步行动加载失败",
        });
      });
    return () => {
      active = false;
    };
  }, [reloadKey]);

  if (state.status === "loading") {
    return (
      <div className="mx-auto w-full max-w-[1040px] space-y-5 px-5 py-8 sm:px-8 lg:py-12" aria-busy="true">
        <div className="h-[300px] animate-pulse rounded-[16px] border border-line bg-white" />
        <span className="sr-only">正在决定下一步…</span>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="mx-auto w-full max-w-[1040px] space-y-4 px-5 py-8 sm:px-8 lg:py-12">
        <ErrorBanner message={state.message} onRetry={() => { setState({ status: "loading" }); setReloadKey((key) => key + 1); }} />
        <Link href="/materials" className="inline-flex rounded-[8px] border border-line-strong bg-white px-4 py-2 text-body-sm font-medium text-ink-secondary">先整理学习资料</Link>
      </div>
    );
  }

  if (state.status === "empty") {
    return (
      <div className="mx-auto w-full max-w-[1040px] px-5 py-8 sm:px-8 lg:py-12">
        <section className="rounded-[16px] border border-line bg-white px-7 py-14 text-center">
          <p className="text-caption font-semibold uppercase tracking-[0.12em] text-ink-muted">从你的资料开始</p>
          <h2 className="mt-3 text-[24px] font-semibold tracking-[-0.035em] text-ink">先建立一个学习空间</h2>
          <p className="mx-auto mt-2 max-w-lg text-[13px] leading-6 text-ink-secondary">上传教材、讲义、试卷或作业后，苏问才能基于真实资料安排学习。</p>
          <Link href="/materials" className="mt-6 inline-flex h-10 items-center rounded-[8px] bg-ink px-5 text-[13px] font-semibold text-white">前往资料</Link>
        </section>
      </div>
    );
  }

  const [primary, ...secondary] = state.items;
  return (
    <div className="mx-auto w-full max-w-[1040px] space-y-6 px-5 py-8 sm:px-8 lg:py-12">
      <header>
        <p className="text-caption font-semibold uppercase tracking-[0.12em] text-ink-muted">基于你的资料与真实学习证据</p>
        <h2 className="mt-2 text-[26px] font-semibold tracking-[-0.04em] text-ink">现在只做这一件事</h2>
      </header>

      <section className="overflow-hidden rounded-[16px] border border-line bg-white shadow-card">
        <div className="p-6 sm:p-8">
          <span className="inline-flex rounded-full bg-brand-50 px-2.5 py-1 text-caption font-semibold text-brand-700">
            {ACTION_LABELS[primary.type] ?? "下一步"}
          </span>
          <h3 className="mt-5 text-[24px] font-semibold leading-9 tracking-[-0.03em] text-ink">
            <MathText text={primary.title} />
          </h3>
          <div className="mt-2 max-w-2xl text-[13px] leading-6 text-ink-secondary">
            <MathText text={primary.reason} />
          </div>
          <Link href={focusActionHref(primary.href)} className="mt-7 inline-flex h-11 items-center gap-2 rounded-[9px] bg-ink px-5 text-[13px] font-semibold text-white transition-colors hover:bg-black">
            开始这一项
            <ArrowIcon />
          </Link>
        </div>

        {secondary.length > 0 && (
          <details className="border-t border-line">
            <summary className="cursor-pointer px-6 py-4 text-body-sm font-medium text-ink-secondary hover:bg-surface-muted sm:px-8">
              其他可做 · {secondary.length} 项
            </summary>
            <ul className="divide-y divide-line border-t border-line">
              {secondary.map((item) => (
                <li key={`${item.type}-${item.href}`}>
                  <Link href={focusActionHref(item.href)} className="flex items-center gap-4 px-6 py-4 transition-colors hover:bg-surface-muted sm:px-8">
                    <span className="min-w-0 flex-1">
                      <span className="block text-[13px] font-medium text-ink"><MathText text={item.title} singleLine /></span>
                      <span className="mt-1 block truncate text-body-sm text-ink-muted"><MathText text={item.reason} singleLine /></span>
                    </span>
                    <span className="shrink-0 text-body-sm text-brand-700">开始 →</span>
                  </Link>
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>

      <TodayPlanSection />

      <ReviewCountStrip />

      <p className="text-center text-caption text-ink-muted">阅读、普通对话和完成任务不会自动改变掌握度；真实作答与受控验证才会形成学习证据。</p>
      <WeeklySummary />
    </div>
  );
}
