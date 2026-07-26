// 品牌视觉：Logo 内联 SVG（对话气泡 + 上升学习曲线 + 琥珀星点）。
// 全部内联绘制，零外部图片/字体依赖，断网可演示。
// 说明：多实例共用同名 gradient id 是安全的——defs 内容完全相同，渲染一致。

import type { ReactNode } from "react";

/**
 * 品牌标：对话气泡（indigo→violet 渐变）内嵌上升学习曲线，端点缀琥珀四角星。
 * - `size`：mark 边长（px）
 * - `withWordmark`：追加 "EduAgents" 字标（颜色继承 currentColor，深色底传 text-white）
 */
export function BrandMark({
  size = 28,
  withWordmark = false,
  className = "",
}: {
  size?: number;
  withWordmark?: boolean;
  className?: string;
}) {
  const mark = (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      aria-hidden
      className="shrink-0"
    >
      <defs>
        <linearGradient
          id="ea-mark-g"
          x1="6"
          y1="4"
          x2="42"
          y2="44"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="#6366f1" />
          <stop offset="1" stopColor="#8b5cf6" />
        </linearGradient>
      </defs>
      {/* 对话气泡 */}
      <path
        d="M24 4C12.95 4 4 12.06 4 22c0 5.58 2.82 10.57 7.24 13.87L9.5 43.5l8.6-3.02c1.87.47 3.85.72 5.9.72 11.05 0 20-8.06 20-18S35.05 4 24 4Z"
        fill="url(#ea-mark-g)"
      />
      {/* 上升学习曲线 */}
      <path
        d="M13 28.5l6.2-6.2 5 3.4 7.3-8.2"
        stroke="#fff"
        strokeWidth="3.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* 琥珀星点：抵达的成就 */}
      <path
        d="M35.5 10l1.5 3.4 3.4 1.5-3.4 1.5-1.5 3.4-1.5-3.4-3.4-1.5 3.4-1.5Z"
        fill="#fbbf24"
      />
    </svg>
  );

  return (
    <span className={`inline-flex items-center gap-2 ${className}`.trim()}>
      {mark}
      {withWordmark && (
        <span
          className="font-semibold tracking-tight text-current"
          style={{ fontSize: Math.round(size * 0.62) }}
        >
          EduAgents
        </span>
      )}
    </span>
  );
}

/** 认证页左栏插画：书本 → 上升曲线 → 知识节点网络，几何抽象风（深色底用） */
function HeroIllustration() {
  return (
    <svg
      viewBox="0 0 480 340"
      fill="none"
      aria-hidden
      className="w-full max-w-md"
    >
      {/* 知识节点连线 */}
      <g stroke="rgba(199,210,254,0.35)" strokeWidth="1.5">
        <path d="M330 96 392 138M392 138 360 196M360 196 296 160M296 160 330 96M392 138 436 108M360 196 420 226" />
      </g>
      {/* 知识节点 */}
      <circle cx="392" cy="138" r="18" fill="rgba(251,191,36,0.16)" />
      <circle cx="392" cy="138" r="9" fill="#fbbf24" />
      <circle cx="330" cy="96" r="7" fill="#a5b4fc" />
      <circle cx="296" cy="160" r="6" fill="#c7d2fe" />
      <circle cx="360" cy="196" r="7" fill="#818cf8" />
      <circle cx="436" cy="108" r="5" fill="rgba(199,210,254,0.7)" />
      <circle cx="420" cy="226" r="5" fill="rgba(199,210,254,0.7)" />

      {/* 对话气泡（几何玻璃质感） */}
      <rect
        x="64"
        y="64"
        width="168"
        height="104"
        rx="20"
        fill="rgba(255,255,255,0.09)"
        stroke="rgba(199,210,254,0.45)"
        strokeWidth="1.5"
      />
      <path d="M92 176l-10 22 26-13Z" fill="rgba(255,255,255,0.14)" />
      <rect x="84" y="88" width="96" height="10" rx="5" fill="rgba(199,210,254,0.55)" />
      <rect x="84" y="110" width="128" height="10" rx="5" fill="rgba(199,210,254,0.35)" />
      <rect x="84" y="132" width="72" height="10" rx="5" fill="rgba(199,210,254,0.35)" />

      {/* 上升曲线：从书本升向知识网络（琥珀圆点轨迹） */}
      <path
        d="M118 252C180 240 224 208 268 180c36-23 76-34 108-38"
        stroke="#fbbf24"
        strokeOpacity="0.75"
        strokeWidth="3"
        strokeLinecap="round"
        strokeDasharray="0.5 12"
      />

      {/* 打开的书 */}
      <path
        d="M104 236c26-14 62-16 96-8v66c-34-8-70-6-96 8Z"
        fill="rgba(255,255,255,0.12)"
        stroke="rgba(199,210,254,0.5)"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M296 236c-26-14-62-16-96-8v66c34-8 70-6 96 8Z"
        fill="rgba(255,255,255,0.07)"
        stroke="rgba(199,210,254,0.5)"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M124 246c20-7 40-9 60-7M124 264c20-7 40-9 60-7M216 239c22-2 40 0 58 7M216 257c22-2 40 0 58 7"
        stroke="rgba(199,210,254,0.35)"
        strokeWidth="1.5"
        strokeLinecap="round"
      />

      {/* 星点点缀 */}
      <path d="M52 40l2.5 5.5L60 48l-5.5 2.5L52 56l-2.5-5.5L44 48l5.5-2.5Z" fill="#fbbf24" opacity="0.9" />
      <path d="M258 44l1.8 4 4 1.8-4 1.8-1.8 4-1.8-4-4-1.8 4-1.8Z" fill="rgba(199,210,254,0.8)" />
      <circle cx="440" cy="60" r="3" fill="rgba(199,210,254,0.5)" />
      <circle cx="40" cy="120" r="2.5" fill="rgba(199,210,254,0.4)" />
    </svg>
  );
}

const HERO_FEATURES = [
  "RAG 知识库问答，每个结论都有引用来源",
  "苏格拉底式辅导，引导思考而非直接给答案",
  "遗忘曲线复习 + 学情报告，随学习自动生成",
];

function FeatureCheck(): ReactNode {
  return (
    <svg viewBox="0 0 20 20" fill="none" aria-hidden className="mt-0.5 h-4 w-4 shrink-0">
      <circle cx="10" cy="10" r="9" fill="rgba(251,191,36,0.15)" stroke="rgba(251,191,36,0.6)" />
      <path
        d="M6.5 10.5l2.3 2.3L14 7.5"
        stroke="#fcd34d"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** 登录/注册页左侧品牌 hero 区（lg 以上显示；移动端由页面只留 logo） */
export function AuthHero() {
  return (
    <aside className="relative hidden overflow-hidden bg-aurora-deep text-white lg:flex lg:flex-col lg:justify-between lg:p-10">
      <BrandMark size={32} withWordmark className="relative z-10 text-white" />

      <div className="relative z-10 my-8 flex flex-col items-start gap-8">
        <div className="animate-fade-up">
          <HeroIllustration />
        </div>
        <div className="animate-fade-up" style={{ animationDelay: "120ms" }}>
          <h2 className="text-2xl font-semibold leading-snug tracking-tight xl:text-3xl">
            让每个学生，都拥有
            <br />
            自己的 <span className="text-accent-300">AI 助教团队</span>
          </h2>
          <ul className="mt-6 space-y-3 text-sm text-indigo-100/90">
            {HERO_FEATURES.map((f) => (
              <li key={f} className="flex items-start gap-2.5">
                <FeatureCheck />
                <span>{f}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <p className="relative z-10 text-xs text-indigo-200/60">
        EduAgents · 多智能体驱动的个性化学习平台
      </p>
    </aside>
  );
}
