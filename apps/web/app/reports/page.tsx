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
  LoadingState,
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

/** 薄弱点卡片：掌握度 + 错题数 + 归因链（可能源于《前置 KC》未掌握）。 */
function WeakPointCard({ wp }: { wp: ReportWeakPoint }) {
  return (
    <li className="rounded-xl border border-slate-200 bg-white p-3">
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
      {wp.possible_root_cause.length > 0 && (
        <div className="mt-2 space-y-1">
          {wp.possible_root_cause.map((rc) => (
            <p key={rc.kc_id} className="flex items-center gap-1 text-xs text-slate-500">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-3.5 w-3.5 shrink-0 text-violet-500"
                aria-hidden
              >
                <path d="M9 14 4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />
              </svg>
              可能源于《{rc.name}》未掌握（{fmtPct(rc.p_mastery)}），建议先补前置
            </p>
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
        <LoadingState text="正在生成学情报告…" />
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
      {!hasAnyData && (
        <EmptyState
          title="还没有足够的学习数据"
          description="完成几次练习或辅导后，这里会生成你的学情报告"
        />
      )}

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
