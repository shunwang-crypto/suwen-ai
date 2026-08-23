

"use client";

// 学情报告（F7）：掌握度雷达（章节聚合轴 + 知识库切换 tab；轴数<5 或移动端降级条形图，
// 防轴标签重叠）+ 30 天趋势（答题数/正确率双系列，纯 SVG、两联面板共享横轴——不做双 Y 轴）
// + 薄弱点卡片（按科目分节，「红线/待巩固」两口径分开计数，节内 p 升序 top5 + 展开全部）
// + 可执行建议（带跳转）+ narrative 三段式诊断（统一由 ModelMarkdown 渲染）。

import Link from "next/link";

import {fmtPct} from "@/components/common";
import type {ReportWeakPoint} from "@/lib/types";

// ---- 后端可选字段与空值降级 ----

/** 薄弱点：分科目契约附带 subject；缺失时降级为单节展示。 */
type SubjectWeakPoint = ReportWeakPoint & { subject?: string | null };

/** 每节薄弱点默认展示条数（p 升序 top5，其余「展开全部」）。 */

/** 30 天趋势：上联答题数（柱），下联正确率（折线，固定 0–100% 刻度），共享横轴。 */
export function WeakPointCard({
  wp,
  isRedline,
  isUnknown,
  redPct,
  consolidatePct,
}: {
  wp: SubjectWeakPoint;
  isRedline: boolean;
  isUnknown: boolean;
  redPct: number;
  consolidatePct: number;
}) {
  const mastery = wp.p_mastery;
  return (
    <li className="rounded-2xl border border-line bg-white p-3 transition-all duration-200 hover:-translate-y-0.5 hover:border-amber-200 hover:shadow-[0_8px_24px_-8px_rgba(245,158,11,0.28)] motion-reduce:hover:translate-y-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink" title={wp.name}>
          {wp.name}
        </span>
        <span
          className={`rounded-full px-2 py-0.5 text-caption font-medium ${
            isUnknown
              ? "bg-surface-muted text-ink-secondary"
              : isRedline
                ? "bg-rose-50 text-rose-700"
                : "bg-amber-50 text-amber-700"
          }`}
          title={
            isUnknown
              ? "历史报告没有留下可验证的掌握度证据"
              : isRedline
              ? `掌握度低于红线（${redPct}%）`
              : `掌握度低于巩固线（${consolidatePct}%）但已过红线`
          }
        >
          {isUnknown ? "待验证" : isRedline ? "红线" : "待巩固"}
        </span>
        <span className="rounded-full bg-surface-muted px-2 py-0.5 font-mono text-caption text-ink-secondary">
          掌握度 {mastery == null ? "未知" : fmtPct(mastery)}
        </span>
        {wp.error_count > 0 && (
          <span className="rounded-full bg-red-50 px-2 py-0.5 text-caption text-red-700">
            {wp.error_count} 道错题
          </span>
        )}
      </div>
      <div className="mt-2 h-2 rounded-full bg-surface-muted">
        <div
          className={`h-full rounded-full ${isUnknown ? "bg-surface-muted" : isRedline ? "bg-rose-500" : "bg-amber-500"}`}
          style={{ width: mastery == null ? "0%" : `${Math.max(mastery * 100, 2)}%` }}
        />
      </div>
      <div className="mt-2 flex justify-end">
        <Link
          href={`/practice/session?mode=quiz&from=${encodeURIComponent("/growth")}&weak=1`}
          title="按画像定向出题：薄弱点自动进卷（掌握度最低的优先）"
          className="rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1 text-caption font-medium text-amber-700 hover:border-amber-300 hover:bg-amber-100"
        >
          针对此点练习
        </Link>
      </div>
      {wp.possible_root_cause.length > 0 && (
        <div className="mt-2.5 space-y-2 border-t border-dashed border-line pt-2.5">
          {wp.possible_root_cause.map((rc) => (
            <div key={rc.kc_id}>
              {/* 归因可视链条：前置 KC → 当前 KC 箭头卡片 */}
              <div className="flex items-center gap-1.5">
                <span
                  className="flex min-w-0 flex-1 flex-col rounded-lg border border-violet-200 bg-violet-50/70 px-2 py-1"
                  title={`前置知识点：${rc.name}`}
                >
                  <span className="truncate text-caption font-medium text-violet-800">
                    {rc.name}
                  </span>
                  <span className="font-mono text-caption text-violet-500">
                    前置 · {rc.p_mastery == null ? "未知" : fmtPct(rc.p_mastery)}
                  </span>
                </span>
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-4 w-4 shrink-0 text-ink-muted"
                  aria-hidden
                >
                  <path d="M5 12h14m-6-6 6 6-6 6" />
                </svg>
                <span
                  className="flex min-w-0 flex-1 flex-col rounded-lg border border-amber-200 bg-amber-50/70 px-2 py-1"
                  title={`当前薄弱点：${wp.name}`}
                >
                  <span className="truncate text-caption font-medium text-amber-800">
                    {wp.name}
                  </span>
                  <span className="font-mono text-caption text-amber-500">
                    当前 · {mastery == null ? "未知" : fmtPct(mastery)}
                  </span>
                </span>
              </div>
              <p className="mt-1 text-caption text-ink-muted">
                可能源于《{rc.name}》未掌握（{rc.p_mastery == null ? "未知" : fmtPct(rc.p_mastery)}），建议先补前置
              </p>
            </div>
          ))}
        </div>
      )}
    </li>
  );
}

/** 按科目分组（subject 缺失时全部归入单节，key=""）。 */
interface WeakGroup {
  key: string;
  items: SubjectWeakPoint[];
  red: number;
  consolidate: number;
  unknown: number;
}

export function groupWeakPoints(points: SubjectWeakPoint[], redThreshold: number): WeakGroup[] {
  const map = new Map<string, WeakGroup>();
  for (const wp of points) {
    const key = wp.subject?.trim() ?? "";
    let g = map.get(key);
    if (!g) {
      g = { key, items: [], red: 0, consolidate: 0, unknown: 0 };
      map.set(key, g);
    }
    g.items.push(wp);
    if (wp.p_mastery == null) g.unknown += 1;
    else if (wp.p_mastery < redThreshold) g.red += 1;
    else g.consolidate += 1;
  }
  const groups = [...map.values()];
  for (const g of groups) {
    g.items.sort((a, b) => {
      if (a.p_mastery == null) return 1;
      if (b.p_mastery == null) return -1;
      return a.p_mastery - b.p_mastery;
    });
  }
  return groups;
}
