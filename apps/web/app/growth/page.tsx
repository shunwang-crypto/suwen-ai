import Link from "next/link";

import { CurrentLearningState } from "@/components/growth/current-state";
import { HistoryTrends } from "@/components/growth/history-trends";

export default function GrowthPage() {
  return (
    <div className="pb-10">
      <header className="mx-auto flex w-full max-w-[1180px] flex-wrap items-end justify-between gap-4 px-5 pb-1 pt-7 sm:px-7 lg:px-8">
        <div>
          <p className="text-caption font-semibold uppercase tracking-[0.12em] text-ink-muted">真实学习证据</p>
          <h1 className="mt-2 text-[26px] font-semibold tracking-[-0.04em] text-ink">成长</h1>
          <p className="mt-1.5 text-body-sm leading-5 text-ink-secondary">先看当前状态，再理解一段时间内发生的变化。</p>
        </div>
        <Link href="/today" className="text-body-sm font-medium text-ink-secondary underline-offset-2 hover:text-brand-700 hover:underline">返回今日</Link>
      </header>
      <CurrentLearningState />
      <div className="mx-auto my-8 h-px w-full max-w-[1180px] bg-line" />
      <section aria-labelledby="growth-history-title">
        <div className="mx-auto w-full max-w-[1180px] px-5 sm:px-7 lg:px-8">
          <h2 id="growth-history-title" className="text-[20px] font-semibold tracking-[-0.03em] text-ink">历史趋势</h2>
          <p className="mt-1 text-body-sm text-ink-muted">练习、复习与掌握度在最近一段时间的变化。</p>
        </div>
        <HistoryTrends />
      </section>
    </div>
  );
}
