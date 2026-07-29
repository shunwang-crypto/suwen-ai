"use client";

// 学情报告（F7）：掌握度雷达（章节聚合轴 + 知识库切换 tab；轴数<5 或移动端降级条形图，
// 防轴标签重叠）+ 30 天趋势（答题数/正确率双系列，纯 SVG、两联面板共享横轴——不做双 Y 轴）
// + 薄弱点卡片（按科目分节，「红线/待巩固」两口径分开计数，节内 p 升序 top5 + 展开全部）
// + 可执行建议（带跳转）+ narrative 三段式诊断（复用 chat 同款 MathText 渲染 + **加粗**）。

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  SkeletonPage,
  Spinner,
  fmtPct,
} from "@/components/common";
import { MasteryRadar, RADAR_MAX_DIMS } from "@/components/radar";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type {
  KbOut,
  LearningReportResponse,
  ReportRadarItem,
  ReportTrendPoint,
  ReportWeakPoint,
} from "@/lib/types";

// ---- 后端并行契约（可选字段 + 空值降级：后端未上线时按旧响应正常工作）----

/** 章节聚合轴的下钻明细项（?kb_id= 时每轴附带）。 */
interface RadarAxisKcDetail {
  kc_id?: string;
  name?: string;
  p_mastery?: number | null;
}

/** 雷达轴：章节聚合模式附带 kc_count/weak_count/kcs；KC 模式与旧契约一致。 */
type RadarAxis = ReportRadarItem & {
  kc_count?: number | null;
  weak_count?: number | null;
  kcs?: RadarAxisKcDetail[] | null;
};

/** 薄弱点：分科目契约附带 subject；缺失时降级为单节展示。 */
type SubjectWeakPoint = ReportWeakPoint & { subject?: string | null };

/** 报告响应扩展：axes_kind（"chapter"=章节聚合轴）与「待巩固」阈值。 */
type ScaledReportResponse = LearningReportResponse & {
  axes_kind?: string | null;
  consolidate_threshold?: number | null;
  /** 待巩固档（红线以上、未达定向出题线），与 weak_points 同结构 */
  consolidate_points?: SubjectWeakPoint[] | null;
  report_status?: "pending" | "generating" | "completed" | "unavailable" | "failed";
  report_updated_at?: string | null;
  cached?: boolean;
};

/** 每节薄弱点默认展示条数（p 升序 top5，其余「展开全部」）。 */
const WEAK_TOP_N = 5;

/** 30 天趋势：上联答题数（柱），下联正确率（折线，固定 0–100% 刻度），共享横轴。 */
function TrendChart({ trend }: { trend: ReportTrendPoint[] }) {
  const hasData = trend.some((t) => t.attempts > 0);
  if (!hasData) {
    return (
      <p className="py-6 text-center text-xs text-slate-400">
        近 30 天暂无作答记录，完成练习后这里会出现趋势
      </p>
    );
  }

  const w = 640;
  const panelH = 96;
  const pad = { l: 34, r: 14, t: 14, b: 8 };
  const axisH = 18; // 底部日期标签带
  const innerW = w - pad.l - pad.r;
  const innerH = panelH - pad.t - pad.b;
  const n = trend.length;
  const slot = innerW / n;
  const xc = (i: number) => pad.l + slot * (i + 0.5);

  const maxAttempts = Math.max(...trend.map((t) => t.attempts), 1);
  const yAttempts = (v: number) => pad.t + innerH - (v / maxAttempts) * innerH;
  const yAcc = (v: number) => pad.t + innerH - v * innerH;

  // 正确率折线按连续非 null 段绘制（无作答日断线，不伪造 0%）
  const segments: number[][] = [];
  let cur: number[] = [];
  trend.forEach((t, i) => {
    if (t.accuracy != null) {
      cur.push(i);
    } else if (cur.length > 0) {
      segments.push(cur);
      cur = [];
    }
  });
  if (cur.length > 0) segments.push(cur);

  const barW = Math.max(slot * 0.55, 3);

  return (
    <div className="overflow-x-auto">
      <div className="min-w-120">
        {/* 上联：答题数（单系列，标题即图例） */}
        <p className="mb-1 flex items-center gap-1.5 text-xs text-slate-500">
          <span className="h-2 w-2 rounded-full bg-indigo-500" aria-hidden />
          每日答题数
        </p>
        <svg
          viewBox={`0 0 ${w} ${panelH}`}
          className="block w-full"
          role="img"
          aria-label="近 30 天每日答题数"
        >
          <line
            x1={pad.l}
            y1={pad.t + innerH}
            x2={w - pad.r}
            y2={pad.t + innerH}
            stroke="#e2e8f0"
            strokeWidth={1}
          />
          <text x={pad.l - 6} y={pad.t + 4} fontSize={10} fill="#94a3b8" textAnchor="end">
            {maxAttempts}
          </text>
          <line
            x1={pad.l}
            y1={pad.t}
            x2={w - pad.r}
            y2={pad.t}
            stroke="#f1f5f9"
            strokeWidth={1}
          />
          {trend.map((t, i) =>
            t.attempts > 0 ? (
              <rect
                key={t.date}
                x={xc(i) - barW / 2}
                y={yAttempts(t.attempts)}
                width={barW}
                height={pad.t + innerH - yAttempts(t.attempts)}
                rx={1.5}
                fill="#6366f1"
              >
                <title>
                  {t.date}：{t.attempts} 题（对 {t.correct}）
                </title>
              </rect>
            ) : null,
          )}
        </svg>

        {/* 下联：正确率（固定 0–100%） */}
        <p className="mb-1 mt-2 flex items-center gap-1.5 text-xs text-slate-500">
          <span className="h-2 w-2 rounded-full bg-emerald-500" aria-hidden />
          每日正确率（无作答日断线）
        </p>
        <svg
          viewBox={`0 0 ${w} ${panelH + axisH}`}
          className="block w-full"
          role="img"
          aria-label="近 30 天每日正确率"
        >
          {[0, 0.5, 1].map((v) => (
            <g key={v}>
              <line
                x1={pad.l}
                y1={yAcc(v)}
                x2={w - pad.r}
                y2={yAcc(v)}
                stroke={v === 0 ? "#e2e8f0" : "#f1f5f9"}
                strokeWidth={1}
              />
              <text
                x={pad.l - 6}
                y={yAcc(v) + 3.5}
                fontSize={10}
                fill="#94a3b8"
                textAnchor="end"
              >
                {Math.round(v * 100)}%
              </text>
            </g>
          ))}
          {segments.map((seg) =>
            seg.length > 1 ? (
              <polyline
                key={`seg-${seg[0]}`}
                points={seg
                  .map((i) => `${xc(i).toFixed(1)},${yAcc(trend[i].accuracy ?? 0).toFixed(1)}`)
                  .join(" ")}
                fill="none"
                stroke="#10b981"
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            ) : null,
          )}
          {trend.map((t, i) =>
            t.accuracy != null ? (
              <circle
                key={t.date}
                cx={xc(i)}
                cy={yAcc(t.accuracy)}
                r={2.8}
                fill="#10b981"
                stroke="#fff"
                strokeWidth={1.2}
              >
                <title>
                  {t.date}：正确率 {fmtPct(t.accuracy)}（{t.correct}/{t.attempts}）
                </title>
              </circle>
            ) : null,
          )}
          {/* 首尾日期标签（共享横轴，只标一次） */}
          <text x={pad.l} y={panelH + axisH - 6} fontSize={10} fill="#94a3b8">
            {trend[0].date}
          </text>
          <text
            x={w - pad.r}
            y={panelH + axisH - 6}
            fontSize={10}
            fill="#94a3b8"
            textAnchor="end"
          >
            {trend[n - 1].date}
          </text>
        </svg>
      </div>
    </div>
  );
}

/** 大数字统计卡：渐变数字 + 趋势箭头（近 7 天 vs 前 7 天，均由已拉取的 trend 就地计算）。
 *  薄弱口径分开命名：红线（<红线阈值）为主数字，待巩固另行标注，消除跨页数字打架。 */
function StatTiles({
  trend,
  redCount,
  consolidateCount,
  redPct,
  consolidatePct,
}: {
  trend: ReportTrendPoint[];
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
    gradient: string;
    delta?: { up: boolean; text: string } | null;
    sub?: string | null;
  }[] = [
    {
      key: "attempts",
      label: "近 30 天答题",
      value: String(total),
      gradient: "from-indigo-600 to-violet-500",
      delta:
        attemptsDelta !== 0
          ? {
              up: attemptsDelta > 0,
              text: `${attemptsDelta > 0 ? "+" : ""}${attemptsDelta} 较前 7 天`,
            }
          : null,
    },
    {
      key: "accuracy",
      label: "总体正确率",
      value: acc != null ? fmtPct(acc) : "—",
      gradient: "from-emerald-600 to-teal-500",
      delta:
        accDelta != null && Math.round(accDelta * 100) !== 0
          ? {
              up: accDelta > 0,
              text: `${accDelta > 0 ? "+" : ""}${Math.round(accDelta * 100)}pp 较前 7 天`,
            }
          : null,
    },
    {
      key: "days",
      label: "活跃天数",
      value: `${activeDays}`,
      gradient: "from-sky-600 to-cyan-500",
      delta: null,
    },
    {
      key: "weak",
      label: `红线知识点（<${redPct}%）`,
      value: String(redCount),
      gradient: "from-rose-600 to-orange-500",
      delta: null,
      sub:
        consolidateCount > 0
          ? `另有 ${consolidateCount} 个待巩固（<${consolidatePct}%）`
          : null,
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {tiles.map((t) => (
        <div
          key={t.key}
          className="rounded-2xl border border-slate-200 bg-white px-4 py-3 shadow-[0_4px_20px_-6px_rgba(79,70,229,0.10)] transition-all duration-200 hover:-translate-y-0.5 hover:shadow-[0_8px_26px_-6px_rgba(79,70,229,0.18)] motion-reduce:hover:translate-y-0"
        >
          <p className="text-xs text-slate-500">{t.label}</p>
          <p
            className={`mt-0.5 bg-gradient-to-br ${t.gradient} bg-clip-text font-mono text-2xl font-bold text-transparent`}
          >
            {t.value}
          </p>
          {t.delta && (
            <p
              className={`mt-0.5 flex items-center gap-0.5 text-[11px] font-medium ${
                t.delta.up ? "text-emerald-600" : "text-orange-600"
              }`}
            >
              <TrendArrow up={t.delta.up} />
              {t.delta.text}
            </p>
          )}
          {t.sub && <p className="mt-0.5 text-[11px] text-amber-600">{t.sub}</p>}
        </div>
      ))}
    </div>
  );
}

/** 条形图轴视图：轴数<5 的降级形态，也是移动端的雷达替代（标签 truncate 不重叠）。 */
function AxisBars({ axes }: { axes: RadarAxis[] }) {
  return (
    <ul className="space-y-2">
      {axes.map((a) => {
        const barColor =
          a.p_mastery < 0.4
            ? "bg-rose-400"
            : a.p_mastery < 0.6
              ? "bg-amber-400"
              : "bg-indigo-500";
        return (
          <li key={a.kc_id || a.name} className="flex items-center gap-2 text-xs">
            <span
              className="w-24 shrink-0 truncate text-slate-600 sm:w-32"
              title={a.name}
            >
              {a.name}
            </span>
            <span className="h-2.5 min-w-0 flex-1 rounded-full bg-slate-100">
              <span
                className={`block h-full rounded-full ${barColor}`}
                style={{ width: `${Math.max(a.p_mastery * 100, 2)}%` }}
              />
            </span>
            <span className="w-10 shrink-0 text-right font-mono text-slate-600">
              {fmtPct(a.p_mastery)}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** 章节聚合轴下钻：每轴 KC 数 / 薄弱数徽章 + KC 明细（后端契约字段缺失时不渲染）。 */
function AxisDrilldown({ axes }: { axes: RadarAxis[] }) {
  const rows = axes.filter(
    (a) => a.kc_count != null || a.weak_count != null || (a.kcs?.length ?? 0) > 0,
  );
  if (rows.length === 0) return null;
  return (
    <div className="mt-3 space-y-1.5 border-t border-slate-100 pt-2.5">
      {rows.map((a) => (
        <details
          key={a.kc_id || a.name}
          className="rounded-lg bg-slate-50/70 px-2.5 py-1.5"
        >
          <summary className="flex cursor-pointer flex-wrap items-center gap-1.5 text-xs">
            <span
              className="min-w-0 flex-1 truncate font-medium text-slate-700"
              title={a.name}
            >
              {a.name}
            </span>
            <span className="shrink-0 font-mono text-slate-500">
              {fmtPct(a.p_mastery)}
            </span>
            {a.kc_count != null && (
              <span className="shrink-0 rounded-full bg-slate-200/70 px-1.5 py-0.5 text-[10px] text-slate-600">
                {a.kc_count} 个知识点
              </span>
            )}
            {a.weak_count != null && a.weak_count > 0 && (
              <span className="shrink-0 rounded-full bg-rose-50 px-1.5 py-0.5 text-[10px] font-medium text-rose-600">
                {a.weak_count} 个薄弱
              </span>
            )}
          </summary>
          {a.kcs && a.kcs.length > 0 ? (
            <ul className="mt-1.5 space-y-1 border-t border-slate-200/60 pt-1.5">
              {a.kcs.map((kc, i) => (
                <li
                  key={kc.kc_id ?? `${kc.name}-${i}`}
                  className="flex items-center justify-between gap-2 text-[11px]"
                >
                  <span className="min-w-0 truncate text-slate-600" title={kc.name}>
                    {kc.name ?? "未命名知识点"}
                  </span>
                  <span className="shrink-0 font-mono text-slate-500">
                    {kc.p_mastery != null ? fmtPct(kc.p_mastery) : "—"}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-[11px] text-slate-400">该轴暂无下钻明细</p>
          )}
        </details>
      ))}
    </div>
  );
}

/** 雷达区：轴数<5 降级条形图；≥5 桌面画雷达、移动端用条形图（防轴标签重叠）。 */
function RadarPanel({ axes, isChapter }: { axes: RadarAxis[]; isChapter: boolean }) {
  if (axes.length === 0) {
    return (
      <p className="py-6 text-center text-xs text-slate-400">
        暂无掌握度证据，完成练习后这里会出现掌握度轴
      </p>
    );
  }
  return (
    <>
      {axes.length < 5 ? (
        <AxisBars axes={axes} />
      ) : (
        <>
          <div className="hidden sm:block">
            <MasteryRadar items={axes} />
          </div>
          <div className="sm:hidden">
            <AxisBars axes={axes} />
          </div>
        </>
      )}
      {isChapter && <AxisDrilldown axes={axes} />}
    </>
  );
}

/** 薄弱点卡片：两口径徽章 + 掌握度 + 错题数 + 归因链（可能源于《前置 KC》未掌握）。 */
function WeakPointCard({
  wp,
  isRedline,
  redPct,
  consolidatePct,
}: {
  wp: SubjectWeakPoint;
  isRedline: boolean;
  redPct: number;
  consolidatePct: number;
}) {
  return (
    <li className="rounded-2xl border border-slate-200 bg-white p-3 transition-all duration-200 hover:-translate-y-0.5 hover:border-amber-200 hover:shadow-[0_8px_24px_-8px_rgba(245,158,11,0.28)] motion-reduce:hover:translate-y-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-800" title={wp.name}>
          {wp.name}
        </span>
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${
            isRedline ? "bg-rose-50 text-rose-700" : "bg-amber-50 text-amber-700"
          }`}
          title={
            isRedline
              ? `掌握度低于红线（${redPct}%）`
              : `掌握度低于巩固线（${consolidatePct}%）但已过红线`
          }
        >
          {isRedline ? "红线" : "待巩固"}
        </span>
        <span className="rounded-full bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-600">
          掌握度 {fmtPct(wp.p_mastery)}
        </span>
        {wp.error_count > 0 && (
          <span className="rounded-full bg-red-50 px-2 py-0.5 text-xs text-red-700">
            {wp.error_count} 道错题
          </span>
        )}
      </div>
      <div className="mt-2 h-2 rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full ${isRedline ? "bg-rose-500" : "bg-amber-500"}`}
          style={{ width: `${Math.max(wp.p_mastery * 100, 2)}%` }}
        />
      </div>
      <div className="mt-2 flex justify-end">
        <Link
          href="/quiz?weak=1"
          title="按画像定向出题：薄弱点自动进卷（掌握度最低的优先）"
          className="rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-700 hover:border-amber-300 hover:bg-amber-100"
        >
          针对此点练习
        </Link>
      </div>
      {wp.possible_root_cause.length > 0 && (
        <div className="mt-2.5 space-y-2 border-t border-dashed border-slate-200 pt-2.5">
          {wp.possible_root_cause.map((rc) => (
            <div key={rc.kc_id}>
              {/* 归因可视链条：前置 KC → 当前 KC 箭头卡片 */}
              <div className="flex items-center gap-1.5">
                <span
                  className="flex min-w-0 flex-1 flex-col rounded-lg border border-violet-200 bg-violet-50/70 px-2 py-1"
                  title={`前置知识点：${rc.name}`}
                >
                  <span className="truncate text-xs font-medium text-violet-800">
                    {rc.name}
                  </span>
                  <span className="font-mono text-[10px] text-violet-500">
                    前置 · {fmtPct(rc.p_mastery)}
                  </span>
                </span>
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-4 w-4 shrink-0 text-slate-300"
                  aria-hidden
                >
                  <path d="M5 12h14m-6-6 6 6-6 6" />
                </svg>
                <span
                  className="flex min-w-0 flex-1 flex-col rounded-lg border border-amber-200 bg-amber-50/70 px-2 py-1"
                  title={`当前薄弱点：${wp.name}`}
                >
                  <span className="truncate text-xs font-medium text-amber-800">
                    {wp.name}
                  </span>
                  <span className="font-mono text-[10px] text-amber-500">
                    当前 · {fmtPct(wp.p_mastery)}
                  </span>
                </span>
              </div>
              <p className="mt-1 text-xs text-slate-500">
                可能源于《{rc.name}》未掌握（{fmtPct(rc.p_mastery)}），建议先补前置
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
}

function groupWeakPoints(points: SubjectWeakPoint[], redThreshold: number): WeakGroup[] {
  const map = new Map<string, WeakGroup>();
  for (const wp of points) {
    const key = wp.subject?.trim() ?? "";
    let g = map.get(key);
    if (!g) {
      g = { key, items: [], red: 0, consolidate: 0 };
      map.set(key, g);
    }
    g.items.push(wp);
    if (wp.p_mastery < redThreshold) g.red += 1;
    else g.consolidate += 1;
  }
  const groups = [...map.values()];
  for (const g of groups) g.items.sort((a, b) => a.p_mastery - b.p_mastery);
  return groups;
}

// ---- AI 三段式诊断：复用 chat 同款 MathText 渲染，行内 **加粗** 局部解析 ----

/** 每次调用新建正则实例：全局正则的 lastIndex 是可变状态，模块级共享会串状态。 */
const boldRe = () => /\*\*(.+?)\*\*/g;

/** 行内轻量 markdown：**加粗** → <strong>，其余与 chat 答案同款 MathText（公式/换行）。 */
function MdInline({ text }: { text: string }) {
  const nodes: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  const re = boldRe();
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      nodes.push(<MathText key={`t${last}`} text={text.slice(last, m.index)} />);
    }
    nodes.push(
      <strong key={`b${m.index}`} className="font-semibold text-slate-900">
        <MathText text={m[1]} />
      </strong>,
    );
    last = m.index + m[0].length;
  }
  if (nodes.length === 0) return <MathText text={text} />;
  if (last < text.length) {
    nodes.push(<MathText key={`t${last}`} text={text.slice(last)} />);
  }
  return <>{nodes}</>;
}

/** 三段式标题行（**现状** / ## 归因 / 【建议】 等写法都认）。 */
const NARRATIVE_HEADING_RE =
  /^\s*(?:\*\*|#{1,4}\s*|【)?\s*(现状|归因|建议)\s*(?:\*\*|】)?\s*[:：]?\s*$/;

function parseNarrative(text: string): {
  intro: string;
  sections: { title: string; body: string }[];
} {
  const sections: { title: string; body: string }[] = [];
  const introLines: string[] = [];
  let cur: { title: string; body: string } | null = null;
  for (const line of text.split("\n")) {
    const m = NARRATIVE_HEADING_RE.exec(line);
    if (m) {
      cur = { title: m[1], body: "" };
      sections.push(cur);
      continue;
    }
    if (cur) cur.body += (cur.body ? "\n" : "") + line;
    else introLines.push(line);
  }
  return {
    intro: introLines.join("\n").trim(),
    sections: sections
      .map((s) => ({ title: s.title, body: s.body.trim() }))
      .filter((s) => s.body.length > 0),
  };
}

const SECTION_BADGE_STYLE: Record<string, string> = {
  现状: "bg-sky-50 text-sky-700",
  归因: "bg-amber-50 text-amber-700",
  建议: "bg-emerald-50 text-emerald-700",
};

/** 诊断正文：三段式解析成分节卡；结构异常（无标题行）整段降级渲染，不崩不裸露星号。 */
function DiagnosisBody({ narrative }: { narrative: string }) {
  const { intro, sections } = parseNarrative(narrative);
  if (sections.length === 0) {
    return (
      <p className="text-sm leading-relaxed text-slate-700">
        <MdInline text={narrative.trim()} />
      </p>
    );
  }
  return (
    <div className="space-y-3">
      {intro && (
        <p className="text-sm leading-relaxed text-slate-600">
          <MdInline text={intro} />
        </p>
      )}
      {sections.map((s, i) => (
        <section
          key={`${s.title}-${i}`}
          className="rounded-xl border border-slate-100 bg-slate-50/60 p-3.5"
        >
          <h3 className="text-sm font-semibold text-slate-800">
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                SECTION_BADGE_STYLE[s.title] ?? "bg-slate-100 text-slate-600"
              }`}
            >
              {s.title}
            </span>
          </h3>
          <p className="mt-1.5 text-sm leading-relaxed text-slate-700">
            <MdInline text={s.body} />
          </p>
        </section>
      ))}
    </div>
  );
}

export default function ReportsPage() {
  const [report, setReport] = useState<ScaledReportResponse | null>(null);
  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [kbId, setKbId] = useState("");
  const [loading, setLoading] = useState(true);
  const [diagnosisLoading, setDiagnosisLoading] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [expandedGroups, setExpandedGroups] = useState<Record<string, boolean>>({});

  // 知识库列表（tab 数据源）：失败只隐藏 tab，不影响报告主体
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<KbOut[]>("/api/kb");
        if (alive) setKbs(res);
      } catch {
        // tab 缺席即全站聚合视图，报告仍可用
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        setRefreshError(null);
        const res = await apiJson<ScaledReportResponse>(
          `/api/reports/learning${kbId ? `?kb_id=${encodeURIComponent(kbId)}` : ""}`,
        );
        if (!alive) return;
        setReport(res);
        setError(null);
        setLoading(false);

        // 首屏数据/旧快照已经可见后再刷新。模型慢只影响 AI 诊断卡，不阻塞整页。
        setDiagnosisLoading(true);
        try {
          const refreshed = await apiJson<ScaledReportResponse>(
            `/api/reports/learning/refresh${kbId ? `?kb_id=${encodeURIComponent(kbId)}` : ""}`,
            { method: "POST" },
          );
          if (alive) setReport(refreshed);
        } catch (refreshErr) {
          if (alive) {
            setRefreshError(
              refreshErr instanceof ApiError
                ? refreshErr.message
                : "后台更新失败，当前仍显示最近一次报告",
            );
          }
        } finally {
          if (alive) setDiagnosisLoading(false);
        }
      } catch (err) {
        if (alive) {
          setError(err instanceof ApiError ? err.message : "学情报告加载失败");
        }
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [reloadKey, kbId]);

  const reload = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  const switchKb = (next: string) => {
    if (next === kbId || loading) return;
    setKbId(next);
    setExpandedGroups({});
    setLoading(true);
  };

  if (loading && !report) {
    return (
      <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
        <PageHeader
          title="学情报告"
          subtitle="正在读取最近一次报告，AI 诊断会在页面展示后单独更新"
        />
        <SkeletonPage />
      </div>
    );
  }

  if (error || !report) {
    return (
      <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
        <PageHeader title="学情报告" subtitle="最近一次报告暂时无法读取" />
        <ErrorBanner message={error ?? "学情报告加载失败"} onRetry={reload} />
      </div>
    );
  }

  const { trend, actions, narrative, narrative_note } = report;
  const radarAxes = report.radar as RadarAxis[];
  // 后端两档分开下发（weak_points=红线 / consolidate_points=待巩固），此处合并后
  // 由 groupWeakPoints 按科目分节并各自计数；旧后端无 consolidate_points 时自动降级
  const weakPoints = [
    ...(report.weak_points as SubjectWeakPoint[]),
    ...((report.consolidate_points ?? []) as SubjectWeakPoint[]),
  ];
  // 两口径阈值：红线（后端 weak_threshold 唯一来源）与待巩固（契约新增，缺省 0.6）
  const redThreshold = report.weak_threshold ?? 0.4;
  const consolidateThreshold = report.consolidate_threshold ?? 0.6;
  const redPct = Math.round(redThreshold * 100);
  const consolidatePct = Math.round(consolidateThreshold * 100);

  // 章节聚合轴判定：axes_kind 显式声明，或任一轴带聚合字段（后端未上线时恒为 false）
  const isChapterAxes =
    report.axes_kind === "chapter" ||
    radarAxes.some((a) => a.kc_count != null || (a.kcs?.length ?? 0) > 0);

  const weakGroups = groupWeakPoints(weakPoints, redThreshold);
  const redTotal = weakGroups.reduce((s, g) => s + g.red, 0);
  const consolidateTotal = weakGroups.reduce((s, g) => s + g.consolidate, 0);
  // subject 全缺失时是单节降级形态，不渲染科目标签
  const hasSubjectSections = weakGroups.some((g) => g.key !== "");

  const hasAnyData =
    radarAxes.length > 0 || weakPoints.length > 0 || trend.some((t) => t.attempts > 0);
  const switching = loading && report != null;
  const updatedLabel = report.report_updated_at
    ? new Date(report.report_updated_at).toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  return (
    <div className="mx-auto w-full max-w-[1180px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      <PageHeader
        title="学情报告"
        subtitle={
          <>
            近 30 天作答证据聚合：掌握度雷达、趋势、薄弱点归因与可执行建议
            {updatedLabel && ` · 更新于 ${updatedLabel}`}
            {diagnosisLoading && " · 正在后台检查更新"}
          </>
        }
        action={
          <button
            type="button"
            onClick={reload}
            disabled={loading || diagnosisLoading}
            className="rounded-control bg-ink px-3.5 py-1.5 text-sm font-semibold text-white transition-colors hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading || diagnosisLoading ? "更新中…" : "刷新报告"}
          </button>
        }
      />

      {/* 知识库切换 tab：全部=全站聚合；选中某库时后端按 ?kb_id= 下发章节聚合轴 */}
      {kbs.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="按知识库查看报告">
          <span className="mr-0.5 text-xs text-slate-500">范围：</span>
          {[{ id: "", name: "全部", subject: "" }, ...kbs].map((kb) => {
            const active = kbId === kb.id;
            return (
              <button
                key={kb.id || "all"}
                type="button"
                role="tab"
                aria-selected={active}
                title={kb.subject || undefined}
                onClick={() => switchKb(kb.id)}
                className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                  active
                    ? "border-indigo-600 bg-indigo-600 text-white"
                    : "border-slate-300 bg-white text-slate-600 hover:border-indigo-300 hover:text-indigo-600"
                }`}
              >
                {kb.name}
              </button>
            );
          })}
          {switching && <Spinner className="h-3.5 w-3.5" />}
        </div>
      )}

      <div className="space-y-4">
        {!hasAnyData && (
          <EmptyState
            title="还没有足够的学习数据"
            description="完成几次练习或辅导后，这里会生成你的学情报告"
            illustration="chart"
          />
        )}

        {hasAnyData && (
          <StatTiles
            trend={trend}
            redCount={redTotal}
            consolidateCount={consolidateTotal}
            redPct={redPct}
            consolidatePct={consolidatePct}
          />
        )}

        {hasAnyData && (
          <div className="grid gap-4 lg:grid-cols-2">
            <Card
              title="掌握度雷达"
              extra={
                <span className="text-xs text-slate-400">
                  {isChapterAxes
                    ? `章节聚合 · ${radarAxes.length} 轴`
                    : `按证据活动量 Top ${Math.min(radarAxes.length, RADAR_MAX_DIMS)}`}
                </span>
              }
            >
              <RadarPanel axes={radarAxes} isChapter={isChapterAxes} />
            </Card>

            <Card title="近 30 天趋势">
              <TrendChart trend={trend} />
            </Card>
          </div>
        )}

        {hasAnyData && (
          <Card
            title="薄弱知识点"
            extra={
              <span className="text-xs text-slate-400">
                红线 &lt;{redPct}% · 待巩固 &lt;{consolidatePct}% · 附归因下钻
              </span>
            }
          >
            {weakPoints.length === 0 ? (
              <p className="py-3 text-center text-xs text-slate-400">
                暂无薄弱点，继续保持
              </p>
            ) : (
              <div className="space-y-5">
                {weakGroups.map((g) => {
                  const groupKey = g.key || "__all__";
                  const expanded = Boolean(expandedGroups[groupKey]);
                  const shown = expanded ? g.items : g.items.slice(0, WEAK_TOP_N);
                  const hidden = g.items.length - shown.length;
                  return (
                    <section key={groupKey}>
                      <header className="mb-2 flex flex-wrap items-center gap-2">
                        {hasSubjectSections && (
                          <span className="rounded-lg bg-indigo-50 px-2 py-0.5 text-xs font-semibold text-indigo-700">
                            {g.key || "未分科"}
                          </span>
                        )}
                        <span className="text-xs text-slate-500">
                          红线{" "}
                          <b className="font-semibold text-rose-600">{g.red}</b> 个 ·
                          待巩固{" "}
                          <b className="font-semibold text-amber-600">
                            {g.consolidate}
                          </b>{" "}
                          个
                        </span>
                      </header>
                      <ul className="grid gap-2.5 md:grid-cols-2">
                        {shown.map((wp) => (
                          <WeakPointCard
                            key={wp.kc_id}
                            wp={wp}
                            isRedline={wp.p_mastery < redThreshold}
                            redPct={redPct}
                            consolidatePct={consolidatePct}
                          />
                        ))}
                      </ul>
                      {(hidden > 0 || expanded) && g.items.length > WEAK_TOP_N && (
                        <div className="mt-2 flex justify-center">
                          <button
                            type="button"
                            onClick={() =>
                              setExpandedGroups((prev) => ({
                                ...prev,
                                [groupKey]: !expanded,
                              }))
                            }
                            className="rounded-lg border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-600 hover:border-indigo-300 hover:text-indigo-600"
                          >
                            {expanded
                              ? "收起"
                              : `展开全部 ${g.items.length} 个（还有 ${hidden} 个）`}
                          </button>
                        </div>
                      )}
                    </section>
                  );
                })}
              </div>
            )}
          </Card>
        )}

        <Card title="可执行建议">
          {actions.length === 0 ? (
            <p className="py-3 text-center text-xs text-slate-400">暂无建议</p>
          ) : (
            <ul className="space-y-2">
              {actions.map((a, i) => (
                <li key={i}>
                  <Link
                    href={a.href}
                    className="group flex items-center justify-between gap-3 rounded-xl border border-slate-200 px-3.5 py-2.5 text-sm text-slate-700 transition-colors hover:border-indigo-300 hover:bg-indigo-50/40"
                  >
                    <span className="min-w-0 flex-1">{a.label}</span>
                    <span className="flex shrink-0 items-center gap-1 text-xs font-medium text-indigo-600">
                      去处理
                      <svg
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5"
                        aria-hidden
                      >
                        <path d="M5 12h14m-6-6 6 6-6 6" />
                      </svg>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="AI 诊断（三段式）"
          extra={
            diagnosisLoading ? (
              <span className="flex items-center gap-1.5 text-xs text-ink-muted">
                <Spinner className="h-3.5 w-3.5" />
                后台更新中
              </span>
            ) : report.report_status === "completed" ? (
              <span className="text-xs text-success-600">已更新</span>
            ) : null
          }
        >
          {refreshError && (
            <p className="mb-3 rounded-control bg-amber-50 px-3 py-2 text-xs text-amber-700">
              {refreshError}
            </p>
          )}
          {narrative && narrative.trim() ? (
            <div className={diagnosisLoading ? "opacity-75" : ""}>
              <DiagnosisBody narrative={narrative} />
              {diagnosisLoading && (
                <p className="mt-3 text-xs text-ink-muted">
                  当前先显示最近一次诊断，更新完成后会自动替换。
                </p>
              )}
            </div>
          ) : diagnosisLoading ? (
            <div className="flex items-center gap-3 rounded-control bg-surface-muted px-3.5 py-4">
              <Spinner className="h-5 w-5 shrink-0" />
              <div>
                <p className="text-sm font-medium text-ink">正在生成最新诊断</p>
                <p className="mt-0.5 text-xs text-ink-muted">
                  雷达、趋势和行动建议已经可以查看，你可以继续浏览其它内容。
                </p>
              </div>
            </div>
          ) : (
            <p className="rounded-xl bg-slate-50 px-3.5 py-3 text-xs text-slate-500">
              {narrative_note ?? "暂无诊断"}
              {report.report_status === "unavailable" &&
                "：当前网关模式下不生成诊断文本，以上聚合数据均来自真实作答记录。"}
            </p>
          )}
        </Card>
      </div>
    </div>
  );
}
