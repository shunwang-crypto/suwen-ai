// 数据背书区：三个大数字卡（真实实验结果，诚实口径标注仿真实验）。
// 数字排版遵循 stat tile 规范：同一无衬线字体、semibold、比例数字。

import { Reveal } from "./reveal";

const STATS = [
  {
    value: "3.1×",
    label: "毕业效率",
    desc: "自适应练习 26 题即达标，随机出题需 81 题。",
  },
  {
    value: "100%",
    label: "答案引用可溯源",
    desc: "知识库问答每条回答附出处角标，可回看原文与页码。",
  },
  {
    value: "开源",
    label: "可复现评测",
    desc: "评测脚本、数据与运行方式全部公开，一键复跑。",
  },
];

export function Stats() {
  return (
    <section className="mx-auto w-full max-w-6xl px-5 py-16 md:py-20">
      <Reveal>
        <h2 className="text-center text-2xl font-bold tracking-tight text-slate-900 md:text-3xl">
          数字说话，口径诚实
        </h2>
      </Reveal>
      <div className="mt-10 grid gap-5 md:grid-cols-3">
        {STATS.map((s, i) => (
          <Reveal key={s.label} delay={i * 120}>
            <div className="h-full rounded-2xl border border-slate-200 bg-white p-6 text-center shadow-sm transition-all duration-300 hover:-translate-y-1 hover:border-indigo-200 hover:shadow-lg hover:shadow-indigo-600/10">
              <p className="bg-gradient-to-r from-indigo-600 to-sky-500 bg-clip-text text-5xl font-semibold leading-none tracking-tight text-transparent">
                {s.value}
              </p>
              <p className="mt-3 text-sm font-semibold text-slate-800">
                {s.label}
              </p>
              <p className="mt-1.5 text-sm leading-relaxed text-slate-500">
                {s.desc}
              </p>
            </div>
          </Reveal>
        ))}
      </div>
      <Reveal delay={360}>
        <p className="mt-6 text-center text-xs text-slate-400">
          * 毕业效率数据来自合成学生仿真实验（N = 30 × 10 seeds），非真实用户统计；评测细节见开源仓库。
        </p>
      </Reveal>
    </section>
  );
}
