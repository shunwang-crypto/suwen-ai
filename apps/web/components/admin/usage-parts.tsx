"use client";

// 运营用量图表基元：时间窗选项、金额/整数格式化、统计卡片、条形行、日趋势折线（admin 运营 tab）。

import type {AdminUsageResponse} from "@/lib/types";

export const DAY_OPTIONS = [7, 14, 30];

export function fmtCny(v: number): string {
  return `¥${v.toFixed(v > 0 && v < 1 ? 4 : 2)}`;
}

export function fmtInt(v: number): string {
  return v.toLocaleString("zh-CN");
}

/** 大数卡片。 */
export function StatTile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <p className="text-xs text-slate-400">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">
        {value}
      </p>
      {sub && <p className="mt-0.5 text-xs text-slate-400">{sub}</p>}
    </div>
  );
}

/** 分组条形（纯 div 宽度百分比，单一靛蓝系列）。 */
export function BarRows({
  rows,
}: {
  rows: { key: string; label: string; value: number; detail: string }[];
}) {
  if (rows.length === 0) {
    return <p className="py-3 text-center text-xs text-slate-400">暂无数据</p>;
  }
  const max = Math.max(...rows.map((r) => r.value), 1e-9);
  return (
    <ul className="space-y-2.5">
      {rows.map((r) => (
        <li key={r.key} className="text-xs">
          <div className="mb-1 flex items-baseline justify-between gap-2">
            <span className="min-w-0 truncate font-mono text-slate-600" title={r.label}>
              {r.label}
            </span>
            <span className="shrink-0 text-slate-500">{r.detail}</span>
          </div>
          <div className="h-3.5 rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-indigo-500"
              style={{ width: `${Math.max((r.value / max) * 100, 2)}%` }}
              title={`${r.label}：${fmtCny(r.value)}`}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** 日趋势折线（纯 SVG polyline，成本单轴）。 */
export function DailyTrend({ daily }: { daily: AdminUsageResponse["daily"] }) {
  if (daily.length === 0) {
    return <p className="py-3 text-center text-xs text-slate-400">窗口内暂无调用</p>;
  }
  const w = 640;
  const h = 180;
  const pad = { l: 14, r: 14, t: 12, b: 22 };
  const max = Math.max(...daily.map((d) => d.cost_cny), 1e-9);
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const x = (i: number) =>
    pad.l + (daily.length === 1 ? innerW / 2 : (i / (daily.length - 1)) * innerW);
  const y = (v: number) => pad.t + innerH - (v / max) * innerH;
  const points = daily.map((d, i) => [x(i), y(d.cost_cny)] as const);

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="block min-w-120 w-full"
        role="img"
        aria-label="每日成本趋势"
      >
        {/* 基线与峰值参考线（递弱网格） */}
        <line
          x1={pad.l}
          y1={pad.t + innerH}
          x2={w - pad.r}
          y2={pad.t + innerH}
          stroke="#e2e8f0"
          strokeWidth={1}
        />
        <line
          x1={pad.l}
          y1={pad.t}
          x2={w - pad.r}
          y2={pad.t}
          stroke="#f1f5f9"
          strokeWidth={1}
        />
        <text x={pad.l} y={pad.t - 2} fontSize={10} fill="#94a3b8">
          峰值 {fmtCny(max)}
        </text>

        {daily.length > 1 && (
          <polyline
            points={points.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(" ")}
            fill="none"
            stroke="#4f46e5"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        )}
        {points.map(([px, py], i) => (
          <circle
            key={daily[i].date}
            cx={px}
            cy={py}
            r={3.2}
            fill="#4f46e5"
            stroke="#fff"
            strokeWidth={1.5}
          >
            <title>
              {daily[i].date}：{fmtCny(daily[i].cost_cny)} · {daily[i].calls} 次调用 ·{" "}
              {fmtInt(daily[i].tokens)} tokens
            </title>
          </circle>
        ))}
        {/* 首尾日期标签 */}
        <text x={pad.l} y={h - 6} fontSize={10} fill="#94a3b8">
          {daily[0].date}
        </text>
        {daily.length > 1 && (
          <text x={w - pad.r} y={h - 6} fontSize={10} fill="#94a3b8" textAnchor="end">
            {daily[daily.length - 1].date}
          </text>
        )}
      </svg>
    </div>
  );
}
