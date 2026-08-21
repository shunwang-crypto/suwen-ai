// 三特性区：引用溯源问答 / 薄弱点自适应练习 / 遗忘曲线复习。
// 每项 60x60 内联双色 SVG 图标 + 一句话 + 悬停浮起。

import { Reveal } from "./reveal";

/** 双色图标：主色 indigo-600 描边 + 辅色 sky-400/indigo-200 填充 */
function IconCitation() {
  return (
    <svg viewBox="0 0 60 60" className="h-[60px] w-[60px]" aria-hidden>
      <rect x="10" y="6" width="34" height="44" rx="5" fill="#e0e7ff" />
      <rect
        x="16"
        y="12"
        width="34"
        height="44"
        rx="5"
        fill="#fff"
        stroke="#4f46e5"
        strokeWidth="2.5"
      />
      <path
        d="M23 24h20M23 31h20M23 38h12"
        stroke="#4f46e5"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      <circle cx="45" cy="45" r="10" fill="#38bdf8" />
      <path
        d="m41 45 3 3 5.5-5.5"
        stroke="#fff"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}

function IconAdaptive() {
  return (
    <svg viewBox="0 0 60 60" className="h-[60px] w-[60px]" aria-hidden>
      <circle cx="28" cy="30" r="21" fill="#e0e7ff" />
      <circle
        cx="28"
        cy="30"
        r="21"
        fill="none"
        stroke="#4f46e5"
        strokeWidth="2.5"
      />
      <circle
        cx="28"
        cy="30"
        r="13"
        fill="none"
        stroke="#4f46e5"
        strokeWidth="2.5"
        opacity="0.5"
      />
      <circle cx="28" cy="30" r="5.5" fill="#38bdf8" />
      <path
        d="M52 6 34 24"
        stroke="#38bdf8"
        strokeWidth="3"
        strokeLinecap="round"
      />
      <path
        d="m50 14 2-8-8 2"
        stroke="#38bdf8"
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}

function IconForgetting() {
  return (
    <svg viewBox="0 0 60 60" className="h-[60px] w-[60px]" aria-hidden>
      <path
        d="M8 12v36h44"
        stroke="#4f46e5"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
      <path
        d="M8 16c10 0 8 18 16 18M24 20c8 0 6 14 14 14M38 24c6 0 5 10 12 10"
        stroke="#c7d2fe"
        strokeWidth="2.5"
        strokeLinecap="round"
        fill="none"
      />
      <path
        d="M8 16c12 0 10 26 22 30"
        stroke="#4f46e5"
        strokeWidth="3"
        strokeLinecap="round"
        fill="none"
      />
      <circle cx="46" cy="18" r="11" fill="#38bdf8" />
      <path
        d="M46 12.5V18l4 2.5"
        stroke="#fff"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}

const FEATURES = [
  {
    icon: <IconCitation />,
    title: "引用溯源问答",
    desc: "绑定知识库提问，每条回答附 [N] 引用角标，一键回看原文段落与页码。",
  },
  {
    icon: <IconAdaptive />,
    title: "薄弱点自适应练习",
    desc: "基于真实作答持续积累证据，证据充分后再识别薄弱点并精准加练。",
  },
  {
    icon: <IconForgetting />,
    title: "遗忘曲线复习",
    desc: "按记忆衰减节奏自动排期，到期提醒复习，让每次进步都留得住。",
  },
];

export function Features() {
  return (
    <section className="mx-auto w-full max-w-6xl px-5 py-16 md:py-20">
      <Reveal>
        <h2 className="text-center text-2xl font-bold tracking-tight text-ink md:text-3xl">
          学习闭环的三块基石
        </h2>
        <p className="mx-auto mt-2 max-w-xl text-center text-sm text-ink-muted md:text-base">
          从「答案可信」到「练习对路」再到「记忆不丢」，全程可验证。
        </p>
      </Reveal>
      <div className="mt-10 grid gap-5 md:grid-cols-3">
        {FEATURES.map((f, i) => (
          <Reveal key={f.title} delay={i * 120}>
            <div className="group h-full rounded-2xl border border-line bg-white p-6 shadow-sm transition-all duration-300 hover:-translate-y-1.5 hover:border-indigo-200 hover:shadow-xl hover:shadow-indigo-600/10">
              <div className="transition-transform duration-300 group-hover:-translate-y-0.5">
                {f.icon}
              </div>
              <h3 className="mt-4 text-lg font-semibold text-ink">
                {f.title}
              </h3>
              <p className="mt-1.5 text-sm leading-relaxed text-ink-muted">
                {f.desc}
              </p>
            </div>
          </Reveal>
        ))}
      </div>
    </section>
  );
}
