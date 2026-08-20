// VLSE 流程区：提问 → 暴露薄弱 → 自动出题 → 到期复习。
// 桌面横向 SVG 虚线连接节点，移动端纵向左侧连线；滚动入场逐个 fade-up。

import { Reveal } from "./reveal";

function stepIcon(d: string) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-6 w-6"
      aria-hidden
    >
      <path d={d} />
    </svg>
  );
}

const STEPS = [
  {
    title: "提问",
    desc: "带知识库上下文提问，回答自带出处引用。",
    icon: stepIcon(
      "M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z",
    ),
  },
  {
    title: "暴露薄弱",
    desc: "真实练习、复习与核验后的作答形成掌握度证据，普通追问和反讲只用于理解语境。",
    icon: stepIcon(
      "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0M18 8l4-4m0 4-4-4",
    ),
  },
  {
    title: "自动出题",
    desc: "在证据充分后围绕已观测薄弱点安排针对性练习与讲解。",
    icon: stepIcon(
      "M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2m-6 9 2 2 4-4",
    ),
  },
  {
    title: "到期复习",
    desc: "按遗忘曲线排期回访，巩固后更新掌握度。",
    icon: stepIcon("M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5m4-1v5l3 3"),
  },
];

export function Flow() {
  return (
    <section className="border-y border-slate-200/70 bg-white/60">
      <div className="mx-auto w-full max-w-6xl px-5 py-16 md:py-20">
        <Reveal>
          <h2 className="text-center text-2xl font-bold tracking-tight text-slate-900 md:text-3xl">
            VLSE 学习状态闭环
          </h2>
          <p className="mx-auto mt-2 max-w-xl text-center text-sm text-slate-500 md:text-base">
            四步把「学过」变成「掌握」，每一步都留下可验证的状态。
          </p>
        </Reveal>

        <div className="relative mt-12">
          {/* 桌面：横向 SVG 虚线连接（渐变描边，穿过四个节点圆心） */}
          <svg
            aria-hidden
            className="absolute left-[12.5%] right-[12.5%] top-7 hidden h-1 w-3/4 md:block"
            viewBox="0 0 100 4"
            preserveAspectRatio="none"
          >
            <defs>
              {/* 水平线 bbox 高度为 0，objectBoundingBox 渐变不渲染 → 用 userSpaceOnUse */}
              <linearGradient
                id="vlse-line"
                gradientUnits="userSpaceOnUse"
                x1="0"
                y1="2"
                x2="100"
                y2="2"
              >
                <stop offset="0%" stopColor="#818cf8" />
                <stop offset="100%" stopColor="#38bdf8" />
              </linearGradient>
            </defs>
            <path
              d="M0 2h100"
              stroke="url(#vlse-line)"
              strokeWidth="2"
              strokeDasharray="4 3"
              fill="none"
              vectorEffect="non-scaling-stroke"
            />
          </svg>

          <ol className="relative grid gap-8 md:grid-cols-4 md:gap-5">
            {STEPS.map((s, i) => (
              <li key={s.title} className="relative">
                {/* 移动端：纵向左侧虚线 */}
                {i < STEPS.length - 1 && (
                  <span
                    aria-hidden
                    className="absolute left-7 top-16 h-[calc(100%-1.5rem)] border-l-2 border-dashed border-indigo-200 md:hidden"
                  />
                )}
                <Reveal delay={i * 150}>
                  <div className="flex items-start gap-4 md:flex-col md:items-center md:text-center">
                    <span className="relative z-10 flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl border-2 border-indigo-200 bg-white text-indigo-600 shadow-sm">
                      {s.icon}
                      <span className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-gradient-to-br from-indigo-600 to-sky-500 text-caption font-bold text-white">
                        {i + 1}
                      </span>
                    </span>
                    <div className="min-w-0 pt-1 md:pt-0">
                      <h3 className="text-base font-semibold text-slate-900">
                        {s.title}
                      </h3>
                      <p className="mt-1 text-sm leading-relaxed text-slate-500">
                        {s.desc}
                      </p>
                    </div>
                  </div>
                </Reveal>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  );
}
