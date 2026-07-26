"use client";

// 学情报告（F7）：掌握度雷达（共享组件）+ 30 天趋势（答题数/正确率双系列，
// 纯 SVG、两联面板共享横轴——不做双 Y 轴）+ 薄弱点卡片（错题数 + 前置归因链）
// + 可执行建议（带跳转）+ narrative 三段式诊断（stub 网关下如实显示 note）。

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  SkeletonPage,
  fmtPct,
} from "@/components/common";
import { MasteryRadar, RADAR_MAX_DIMS } from "@/components/radar";
import { ApiError, apiJson } from "@/lib/api";
import type {
  LearningReportResponse,
  ReportTrendPoint,
  ReportWeakPoint,
} from "@/lib/types";

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

/** 大数字统计卡：渐变数字 + 趋势箭头（近 7 天 vs 前 7 天，均由已拉取的 trend 就地计算）。 */
function StatTiles({
  trend,
  weakCount,
}: {
  trend: ReportTrendPoint[];
  weakCount: number;
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
      label: "薄弱知识点",
      value: String(weakCount),
      gradient: "from-amber-600 to-orange-500",
      delta: null,
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
        </div>
      ))}
    </div>
  );
}

/** 薄弱点卡片：掌握度 + 错题数 + 归因链（可能源于《前置 KC》未掌握）。 */
function WeakPointCard({ wp }: { wp: ReportWeakPoint }) {
  return (
    <li className="rounded-2xl border border-slate-200 bg-white p-3 transition-all duration-200 hover:-translate-y-0.5 hover:border-amber-200 hover:shadow-[0_8px_24px_-8px_rgba(245,158,11,0.28)] motion-reduce:hover:translate-y-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-800" title={wp.name}>
          {wp.name}
        </span>
        <span className="rounded-full bg-amber-50 px-2 py-0.5 font-mono text-xs text-amber-700">
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
          className="h-full rounded-full bg-amber-500"
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

export default function ReportsPage() {
  const [report, setReport] = useState<LearningReportResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<LearningReportResponse>("/api/reports/learning");
        if (!alive) return;
        setReport(res);
        setError(null);
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
  }, [reloadKey]);

  const reload = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  if (loading) {
    return (
      <div className="mx-auto w-full max-w-6xl p-4">
        <SkeletonPage />
      </div>
    );
  }

  if (error || !report) {
    return (
      <div className="mx-auto w-full max-w-6xl p-4">
        <ErrorBanner message={error ?? "学情报告加载失败"} onRetry={reload} />
      </div>
    );
  }

  const { radar, trend, weak_points, actions, narrative, narrative_note } = report;
  const hasAnyData =
    radar.length > 0 || weak_points.length > 0 || trend.some((t) => t.attempts > 0);

  return (
    <div className="mx-auto w-full max-w-6xl space-y-4 p-4">
      <PageHeader
        title="学情报告"
        subtitle="近 30 天作答证据聚合：掌握度雷达、趋势、薄弱点归因与可执行建议"
        action={
          <button
            type="button"
            onClick={reload}
            className="rounded-xl bg-indigo-600 px-3.5 py-1.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-700"
          >
            刷新报告
          </button>
        }
      />

      {!hasAnyData && (
        <EmptyState
          title="还没有足够的学习数据"
          description="完成几次练习或辅导后，这里会生成你的学情报告"
          illustration="chart"
        />
      )}

      {hasAnyData && <StatTiles trend={trend} weakCount={weak_points.length} />}

      {hasAnyData && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card
            title="掌握度雷达"
            extra={
              <span className="text-xs text-slate-400">
                按证据活动量 Top {Math.min(radar.length, RADAR_MAX_DIMS)}
              </span>
            }
          >
            <MasteryRadar items={radar} />
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
            <span className="text-xs text-slate-400">掌握度 &lt; 40% · 附归因下钻</span>
          }
        >
          {weak_points.length === 0 ? (
            <p className="py-3 text-center text-xs text-slate-400">
              暂无薄弱点，继续保持
            </p>
          ) : (
            <ul className="grid gap-2.5 md:grid-cols-2">
              {weak_points.map((wp) => (
                <WeakPointCard key={wp.kc_id} wp={wp} />
              ))}
            </ul>
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

      <Card title="AI 诊断（三段式）">
        {narrative ? (
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700">
            {narrative}
          </p>
        ) : (
          <p className="rounded-xl bg-slate-50 px-3.5 py-3 text-xs text-slate-500">
            {narrative_note ?? "暂无诊断"}
            ：当前网关模式下不生成诊断文本，以上聚合数据均来自真实作答记录。
          </p>
        )}
      </Card>
    </div>
  );
}
