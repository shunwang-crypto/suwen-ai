

"use client";

// 学情报告（F7）：掌握度雷达（章节聚合轴 + 知识库切换 tab；轴数<5 或移动端降级条形图，
// 防轴标签重叠）+ 30 天趋势（答题数/正确率双系列，纯 SVG、两联面板共享横轴——不做双 Y 轴）
// + 薄弱点卡片（按科目分节，「红线/待巩固」两口径分开计数，节内 p 升序 top5 + 展开全部）
// + 可执行建议（带跳转）+ narrative 三段式诊断（统一由 ModelMarkdown 渲染）。


import {fmtPct} from "@/components/common";
import type {LearningReportResponse, ReportTrendPoint} from "@/lib/types";

// ---- 后端可选字段与空值降级 ----

/** 每节薄弱点默认展示条数（p 升序 top5，其余「展开全部」）。 */

/** 30 天趋势：上联答题数（柱），下联正确率（折线，固定 0–100% 刻度），共享横轴。 */
export function StatTiles({
  trend,
  metrics,
  redCount,
  consolidateCount,
  redPct,
  consolidatePct,
}: {
  trend: ReportTrendPoint[];
  metrics?: LearningReportResponse["metrics"];
  redCount: number;
  consolidateCount: number;
  redPct: number;
  consolidatePct: number;
}) {
  const sumAttempts = (arr: ReportTrendPoint[]) =>
    arr.reduce((s, t) => s + t.attempts, 0);
  const accOf = (arr: ReportTrendPoint[]): number | null => {
    const a = sumAttempts(arr);
    if (a === 0) return null;
    return arr.reduce((s, t) => s + t.correct, 0) / a;
  };

  const total = sumAttempts(trend);
  const acc = accOf(trend);
  const activeDays = trend.filter((t) => t.attempts > 0).length;
  const last7 = trend.slice(-7);
  const prev7 = trend.slice(-14, -7);
  const attemptsDelta = sumAttempts(last7) - sumAttempts(prev7);
  const acc7 = accOf(last7);
  const accPrev7 = accOf(prev7);
  const accDelta = acc7 != null && accPrev7 != null ? acc7 - accPrev7 : null;
  const attemptDeltaValue = metrics?.attempt_count_delta ?? attemptsDelta;
  const accuracyDeltaValue = metrics?.accuracy_delta ?? accDelta;
  const activeDaysDeltaValue = metrics?.active_days_delta ?? null;

  const TrendArrow = ({ up }: { up: boolean }) => (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`h-3 w-3 ${up ? "" : "rotate-180"}`}
      aria-hidden
    >
      <path d="M12 19V5m-6 6 6-6 6 6" />
    </svg>
  );

  const tiles: {
    key: string;
    label: string;
    value: string;
    delta?: { up: boolean; text: string } | null;
    sub?: string | null;
  }[] = [
    {
      key: "attempts",
      label: "学习投入",
      value: `${metrics?.attempt_count ?? total} 次作答`,
      delta:
        attemptDeltaValue !== 0
          ? {
              up: attemptDeltaValue > 0,
              text: `${attemptDeltaValue > 0 ? "+" : ""}${attemptDeltaValue} 较上期`,
            }
          : null,
    },
    {
      key: "accuracy",
      label: "总体正确率",
      value: (metrics?.accuracy ?? acc) != null ? fmtPct((metrics?.accuracy ?? acc) as number) : "—",
      delta:
        accuracyDeltaValue != null && Math.round(accuracyDeltaValue * 100) !== 0
          ? {
              up: accuracyDeltaValue > 0,
              text: `${accuracyDeltaValue > 0 ? "+" : ""}${Math.round(accuracyDeltaValue * 100)}pp 较上期`,
            }
          : null,
    },
    {
      key: "days",
      label: "活跃天数",
      value: `${metrics?.active_days ?? activeDays} / 30 天`,
      delta:
        activeDaysDeltaValue != null && activeDaysDeltaValue !== 0
          ? {
              up: activeDaysDeltaValue > 0,
              text: `${activeDaysDeltaValue > 0 ? "+" : ""}${activeDaysDeltaValue} 天较上期`,
            }
          : null,
    },
    {
      key: "weak",
      label: "掌握变化",
      value: `+${metrics?.newly_mastered_count ?? 0} 个知识点`,
      delta: null,
      sub:
        metrics
          ? `新掌握 ${metrics.newly_mastered_count} · 滑落 ${metrics.slipped_count}`
          : redCount + consolidateCount > 0
            ? `${redCount} 个红线（<${redPct}%）· ${consolidateCount} 个待巩固（<${consolidatePct}%）`
            : null,
    },
  ];

  return (
    <div className="grid grid-cols-2 overflow-hidden rounded-[14px] border border-line bg-white lg:grid-cols-4">
      {tiles.map((t) => (
        <div
          key={t.key}
          className="border-r border-line bg-white px-4 py-3 last:border-r-0"
        >
          <p className="text-caption text-slate-500">{t.label}</p>
          <p className="mt-1 text-[22px] font-semibold tracking-[-0.03em] text-ink">
            {t.value}
          </p>
          {t.delta && (
            <p
              className={`mt-0.5 flex items-center gap-0.5 text-body-sm font-medium ${
                t.delta.up ? "text-emerald-600" : "text-orange-600"
              }`}
            >
              <TrendArrow up={t.delta.up} />
              {t.delta.text}
            </p>
          )}
          {t.sub && <p className="mt-0.5 text-body-sm text-amber-600">{t.sub}</p>}
        </div>
      ))}
    </div>
  );
}

/** 条形图轴视图：轴数<5 的降级形态，也是移动端的雷达替代（标签 truncate 不重叠）。 */
