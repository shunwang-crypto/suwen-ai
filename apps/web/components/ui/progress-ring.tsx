"use client";

// 环形进度（画像/报告页掌握度可视化）：纯 SVG 圆环 + 数值居中，
// 入场时从 0 动画到目标值（CSS transition stroke-dashoffset）；
// prefers-reduced-motion 下由 motion-reduce:transition-none 直达终值。
// 色阶与既有线性进度条同口径：>=0.7 绿 / >=0.4 靛 / 其余 琥珀。

import { useEffect, useState } from "react";

/** 与画像页线性进度条一致的三档色。 */
export function masteryColor(p: number): string {
  if (p >= 0.7) return "#10b981"; // emerald-500
  if (p >= 0.4) return "#6366f1"; // indigo-500
  return "#f59e0b"; // amber-500
}

export function ProgressRing({
  value,
  size = 48,
  strokeWidth = 5,
  color,
  showValue = true,
  title,
  className,
}: {
  /** 进度 0–1 */
  value: number;
  /** 直径 px */
  size?: number;
  strokeWidth?: number;
  /** 覆盖默认三档色 */
  color?: string;
  /** 是否居中显示百分数 */
  showValue?: boolean;
  /** 悬停提示（<title>） */
  title?: string;
  className?: string;
}) {
  const clamped = Math.max(0, Math.min(1, value));
  const stroke = color ?? masteryColor(clamped);
  const r = (size - strokeWidth) / 2;
  const c = 2 * Math.PI * r;

  // 入场动画：首帧画 0，随后过渡到目标值
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);
  const shown = entered ? clamped : 0;

  return (
    <span
      className={`relative inline-flex shrink-0 items-center justify-center ${className ?? ""}`}
      style={{ width: size, height: size }}
    >
      <svg
        viewBox={`0 0 ${size} ${size}`}
        width={size}
        height={size}
        className="-rotate-90"
        role="img"
        aria-label={title ?? `进度 ${Math.round(clamped * 100)}%`}
      >
        {title && <title>{title}</title>}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="#e2e8f0"
          strokeWidth={strokeWidth}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={stroke}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - shown)}
          className="transition-[stroke-dashoffset] duration-700 ease-out motion-reduce:transition-none"
        />
      </svg>
      {showValue && (
        <span
          className="absolute inset-0 flex items-center justify-center font-mono font-semibold text-ink-secondary"
          style={{ fontSize: Math.max(10, size * 0.24) }}
        >
          {Math.round(clamped * 100)}
          <span className="ml-px" style={{ fontSize: Math.max(8, size * 0.16) }}>
            %
          </span>
        </span>
      )}
    </span>
  );
}
