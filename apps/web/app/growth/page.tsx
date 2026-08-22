"use client";

import Link from "next/link";
import { useState } from "react";

import { CurrentLearningState } from "@/components/growth/current-state";
import { HistoryTrends } from "@/components/growth/history-trends";

// 「当前状态」= 系统此刻基于真实证据的认识；「历史趋势」= 一段时间内的变化。
// 原先两块长仪表盘同页竖直堆叠（雷达/薄弱点各出现两次）造成「挤在一起」，
// 改为客户端 tab 分离：一次只呈现一个，不改路由、不动各自数据加载。
const TABS = [
  { key: "current", label: "当前状态" },
  { key: "history", label: "历史趋势" },
] as const;

export default function GrowthPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]["key"]>("current");

  return (
    <div className="pb-10">
      <header className="mx-auto w-full max-w-[1180px] px-5 pt-7 sm:px-7 lg:px-8">
        <div className="flex flex-wrap items-end justify-between gap-4 pb-1">
          <div>
            <p className="text-caption font-semibold uppercase tracking-[0.12em] text-ink-muted">真实学习证据</p>
            <h1 className="mt-2 text-[26px] font-semibold tracking-[-0.04em] text-ink">成长</h1>
            <p className="mt-1.5 text-body-sm leading-5 text-ink-secondary">先看当前状态，再理解一段时间内发生的变化。</p>
          </div>
          <Link href="/today" className="text-body-sm font-medium text-ink-secondary underline-offset-2 hover:text-brand-700 hover:underline">返回今日</Link>
        </div>
        <div className="mt-4 flex gap-1 border-b border-line" role="tablist" aria-label="成长视图">
          {TABS.map((t) => {
            const on = t.key === tab;
            return (
              <button
                key={t.key}
                type="button"
                role="tab"
                aria-selected={on}
                aria-controls={`growth-panel-${t.key}`}
                id={`growth-tab-${t.key}`}
                onClick={() => setTab(t.key)}
                className={`-mb-px border-b-2 px-3.5 py-2 text-body-sm font-medium transition-colors ${
                  on ? "border-brand-600 text-ink" : "border-transparent text-ink-muted hover:text-ink-secondary"
                }`}
              >
                {t.label}
              </button>
            );
          })}
        </div>
      </header>
      {tab === "current" && (
        <section
          id="growth-panel-current"
          role="tabpanel"
          aria-labelledby="growth-tab-current"
        >
          <CurrentLearningState />
        </section>
      )}
      {tab === "history" && (
        <section
          id="growth-panel-history"
          role="tabpanel"
          aria-labelledby="growth-tab-history"
        >
          <HistoryTrends />
        </section>
      )}
    </div>
  );
}
