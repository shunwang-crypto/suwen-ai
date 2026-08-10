"use client";

// 掌握度雷达图（纯 SVG polygon，≤12 维）：profile 页与学情报告页共享。
// 轴标 KC 名；网格环 25/50/75/100%；数据点 <title> 悬停可读。

import { fmtPct } from "@/components/common";

export interface RadarItem {
  name: string;
  p_mastery: number;
}

export const RADAR_MAX_DIMS = 12;

export function MasteryRadar({ items }: { items: RadarItem[] }) {
  const dims = items.slice(0, RADAR_MAX_DIMS);
  if (dims.length < 3) {
    return (
      <p className="py-8 text-center text-xs text-slate-400">
        至少需要 3 个知识点才能绘制雷达图，先去练习积累证据吧
      </p>
    );
  }
  const size = 340;
  const cx = size / 2;
  const cy = size / 2;
  const radius = 108;
  const labelRadius = radius + 20;
  const n = dims.length;

  const point = (i: number, r: number): [number, number] => {
    const angle = (Math.PI * 2 * i) / n - Math.PI / 2;
    return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
  };

  const ringPath = (ratio: number) =>
    dims
      .map((_, i) => point(i, radius * ratio).map((v) => v.toFixed(1)).join(","))
      .join(" ");

  const dataPoints = dims.map((d, i) => point(i, radius * Math.max(0.02, d.p_mastery)));

  return (
    <div className="relative overflow-x-auto">
      {/* 卡片光晕背景（纯 CSS 径向渐变，不影响 SVG 绘制） */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 mx-auto max-w-90"
        style={{
          background:
            "radial-gradient(circle at 50% 46%, rgba(99,102,241,0.10) 0%, rgba(99,102,241,0.04) 42%, transparent 68%)",
        }}
      />
      <svg
        viewBox={`0 0 ${size} ${size}`}
        className="relative mx-auto block w-full max-w-90"
        role="img"
        aria-label={`掌握度雷达图，共 ${n} 个知识点`}
      >
        {/* 网格环（递弱视觉） */}
        {[0.25, 0.5, 0.75, 1].map((r) => (
          <polygon
            key={r}
            points={ringPath(r)}
            fill="none"
            stroke="#e2e8f0"
            strokeWidth={r === 1 ? 1.2 : 0.8}
          />
        ))}
        {/* 轴线 */}
        {dims.map((_, i) => {
          const [x, y] = point(i, radius);
          return (
            <line
              key={i}
              x1={cx}
              y1={cy}
              x2={x}
              y2={y}
              stroke="#e2e8f0"
              strokeWidth={0.8}
            />
          );
        })}
        {/* 数据多边形 */}
        <polygon
          points={dataPoints.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
          fill="rgba(79, 70, 229, 0.14)"
          stroke="#4f46e5"
          strokeWidth={2}
          strokeLinejoin="round"
        />
        {/* 数据点（白描边与填充分离重叠标记） */}
        {dataPoints.map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r={3.2} fill="#4f46e5" stroke="#fff" strokeWidth={1.5}>
            <title>
              {dims[i].name}：{fmtPct(dims[i].p_mastery)}
            </title>
          </circle>
        ))}
        {/* 轴标签 */}
        {dims.map((d, i) => {
          const [x, y] = point(i, labelRadius);
          const anchor =
            Math.abs(x - cx) < 8 ? "middle" : x > cx ? "start" : "end";
          const name = d.name.length > 7 ? `${d.name.slice(0, 6)}…` : d.name;
          return (
            <text
              key={i}
              x={x}
              y={y + 3}
              textAnchor={anchor}
              fontSize={10.5}
              fill="#64748b"
            >
              <title>{d.name}</title>
              {name}
            </text>
          );
        })}
      </svg>
      {/* 图例：数据面含义 + 网格环刻度说明 */}
      <div className="relative mt-1 flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-body-sm text-slate-400">
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-sm border border-indigo-600"
            style={{ background: "rgba(79, 70, 229, 0.14)" }}
          />
          当前掌握度
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block h-2.5 w-2.5 rounded-sm border border-slate-300"
          />
          网格环 25 / 50 / 75 / 100%
        </span>
      </div>
    </div>
  );
}
