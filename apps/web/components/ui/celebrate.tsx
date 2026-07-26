"use client";

// 庆祝彩带（复习答对且 state=mastered 时触发）：纯 CSS confetti，
// 20 个彩色小方块 keyframes 抛洒 1.2s 后消失；不依赖任何外部资源。
// prefers-reduced-motion：彩带隐藏，降级为静态星形徽章。

const COLORS = [
  "#6366f1", // indigo
  "#10b981", // emerald
  "#f59e0b", // amber
  "#0ea5e9", // sky
  "#f43f5e", // rose
  "#8b5cf6", // violet
];

interface Piece {
  left: number; // 起点横向位置（%）
  dx: number; // 抛洒横向位移 px
  dy: number; // 抛洒纵向位移 px（负值向上后落下由缓动模拟）
  rot: number; // 终点旋转角
  delay: number; // s
  size: number; // px
  color: string;
}

// 预生成 20 个方块（确定性数组，避免每次渲染抖动）
const PIECES: Piece[] = Array.from({ length: 20 }, (_, i) => {
  const t = i / 20;
  return {
    left: 8 + ((i * 37) % 84),
    dx: Math.round(Math.sin(i * 2.399) * 90),
    dy: -140 - ((i * 53) % 90),
    rot: 180 + ((i * 97) % 480) * (i % 2 === 0 ? 1 : -1),
    delay: +(t * 0.28).toFixed(2),
    size: 5 + (i % 3) * 2,
    color: COLORS[i % COLORS.length],
  };
});

const CSS = `
@keyframes edu-confetti-up {
  0% { transform: translate3d(0, 0, 0) rotate(0deg); opacity: 0; }
  12% { opacity: 1; }
  60% { transform: translate3d(calc(var(--cx) * 0.8), var(--cy), 0) rotate(calc(var(--cr) * 0.7)); opacity: 1; }
  100% { transform: translate3d(var(--cx), calc(var(--cy) * -0.15), 0) rotate(var(--cr)); opacity: 0; }
}
.edu-confetti-piece { animation: edu-confetti-up 1.2s cubic-bezier(.2,.65,.45,1) var(--cd) both; }
@media (prefers-reduced-motion: reduce) {
  .edu-confetti-piece { display: none; }
}
`;

/**
 * 用法：放进 relative 容器内即渲染即播（一次性动画，播完自动透明）。
 * 需要重播时由调用方换 key 重挂载。
 */
export function Celebrate({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={`pointer-events-none absolute inset-x-0 bottom-0 top-auto z-10 block h-0 ${className ?? ""}`}
    >
      <style href="edu-celebrate" precedence="default">
        {CSS}
      </style>
      {PIECES.map((p, i) => (
        <span
          key={i}
          className="edu-confetti-piece absolute block rounded-[2px]"
          style={{
            left: `${p.left}%`,
            bottom: 0,
            width: p.size,
            height: p.size,
            background: p.color,
            ["--cx" as string]: `${p.dx}px`,
            ["--cy" as string]: `${p.dy}px`,
            ["--cr" as string]: `${p.rot}deg`,
            ["--cd" as string]: `${p.delay}s`,
          }}
        />
      ))}
      {/* 降级徽章：仅 prefers-reduced-motion 下显示的静态星标 */}
      <span className="absolute -top-8 right-2 hidden h-7 w-7 items-center justify-center rounded-full bg-emerald-500 text-white shadow-md motion-reduce:flex">
        <svg
          viewBox="0 0 24 24"
          fill="currentColor"
          className="h-4 w-4"
          aria-hidden
        >
          <path d="M12 2.6 14.7 8l6 .9-4.3 4.2 1 6-5.4-2.8L6.6 19l1-6L3.4 8.9l6-.9L12 2.6Z" />
        </svg>
      </span>
    </span>
  );
}
