// 落地页 Hero：顶部导航 + 主标语 + 双 CTA + aurora 背景 + 浮动几何粒子
// + 纯 HTML/CSS 手绘的产品界面示意卡（无外部图片/CDN）。

import Link from "next/link";

import { BrandMark } from "@/components/ui/brand";

/** 浮动几何粒子层：11 个纯 CSS/SVG 元素，三档慢速漂浮 */
function Particles() {
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
      {/* 圆点 */}
      <span className="fx-float-1 absolute left-[8%] top-[18%] h-2.5 w-2.5 rounded-full bg-indigo-300/70" />
      <span className="fx-float-2 absolute left-[22%] top-[70%] h-2 w-2 rounded-full bg-sky-300/70" />
      <span className="fx-float-3 absolute right-[12%] top-[24%] h-3 w-3 rounded-full bg-violet-300/60" />
      <span className="fx-float-1 absolute right-[28%] top-[78%] h-2 w-2 rounded-full bg-teal-300/60" />
      {/* 圆环 */}
      <span className="fx-float-2 absolute left-[14%] top-[42%] h-5 w-5 rounded-full border-2 border-indigo-300/50" />
      <span className="fx-float-3 absolute right-[6%] top-[56%] h-6 w-6 rounded-full border-2 border-sky-300/50" />
      {/* 方块 */}
      <span className="fx-float-3 absolute left-[38%] top-[12%] h-3 w-3 rotate-12 rounded-sm bg-sky-200/70" />
      <span className="fx-float-1 absolute right-[40%] top-[64%] h-2.5 w-2.5 -rotate-6 rounded-sm bg-violet-200/70" />
      {/* 三角 / 加号（内联 SVG） */}
      <svg
        className="fx-float-2 absolute left-[58%] top-[20%] h-4 w-4 text-indigo-300/60"
        viewBox="0 0 16 16"
        fill="currentColor"
      >
        <path d="M8 2 14 13H2L8 2Z" />
      </svg>
      <svg
        className="fx-float-1 absolute left-[70%] top-[80%] h-4 w-4 text-teal-300/60"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      >
        <path d="M8 3v10M3 8h10" />
      </svg>
      <svg
        className="fx-float-3 absolute left-[4%] top-[86%] h-3.5 w-3.5 text-sky-300/60"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      >
        <path d="M8 3v10M3 8h10" />
      </svg>
    </div>
  );
}

/** 产品界面示意卡：HTML+CSS 画的简化对话界面（静态内容写死，非截图） */
function ProductMock() {
  return (
    <div className="fx-bob relative mx-auto w-full max-w-md md:rotate-2">
      <div className="rounded-2xl border border-slate-200/80 bg-white/90 shadow-[0_24px_60px_-16px_rgba(79,70,229,0.28)] backdrop-blur">
        {/* 窗口头 */}
        <div className="flex items-center gap-1.5 border-b border-slate-100 px-4 py-2.5">
          <span className="h-2.5 w-2.5 rounded-full bg-rose-300" />
          <span className="h-2.5 w-2.5 rounded-full bg-amber-300" />
          <span className="h-2.5 w-2.5 rounded-full bg-emerald-300" />
          <span className="ml-2 truncate text-xs text-slate-400">
            苏问 · 可溯源学习辅导
          </span>
        </div>
        <div className="space-y-3 p-4">
          {/* 用户气泡 */}
          <div className="flex justify-end">
            <div className="rounded-2xl rounded-br-md bg-indigo-600 px-3.5 py-2 text-xs leading-relaxed text-white">
              什么是贝叶斯定理？
            </div>
          </div>
          {/* 过程时间线 chip */}
          <div className="flex items-center gap-1.5 text-caption text-slate-500">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.4"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-3 w-3 text-emerald-500"
              aria-hidden
            >
              <path d="M20 6 9 17l-5-5" />
            </svg>
            检索 → 自评 → 生成 · 3 步完成
          </div>
          {/* AI 气泡（含引用角标） */}
          <div className="flex justify-start">
            <div className="rounded-2xl rounded-bl-md border border-slate-200 bg-white px-3.5 py-2 text-xs leading-relaxed text-slate-700 shadow-sm">
              贝叶斯定理描述了在已知相关证据时，如何更新事件的概率
              <span className="mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded bg-indigo-100 px-1 text-caption font-semibold text-indigo-700">
                1
              </span>
              ：P(A|B) = P(B|A)·P(A)/P(B)。
            </div>
          </div>
          {/* 引用卡（假数据） */}
          <div className="relative overflow-hidden rounded-xl border border-slate-200 bg-slate-50/80 p-2.5 pl-3.5">
            <span className="absolute inset-y-0 left-0 w-1 bg-gradient-to-b from-indigo-500 to-sky-400" />
            <div className="flex items-center gap-1.5">
              <span className="flex h-4 min-w-4 items-center justify-center rounded bg-indigo-600 px-1 text-caption font-semibold text-white">
                1
              </span>
              <span className="truncate text-body-sm font-medium text-slate-700">
                概率论讲义.pdf
              </span>
              <span className="ml-auto shrink-0 text-caption text-slate-400">
                第 12 页
              </span>
            </div>
            <p className="mt-1 line-clamp-2 border-l-2 border-indigo-200 pl-2 text-caption leading-relaxed text-slate-500">
              条件概率公式 P(A|B) = P(AB)/P(B)，结合全概率公式即可推出贝叶斯定理……
            </p>
          </div>
          {/* 苏格拉底式追问：展示“给出依据后继续引导思考”的产品差异 */}
          <div className="rounded-xl border border-violet-200/80 bg-violet-50/70 px-3 py-2.5">
            <div className="flex items-center gap-1.5 text-caption font-semibold text-violet-700">
              <span className="flex h-4 w-4 items-center justify-center rounded-full bg-violet-600 text-caption text-white">
                问
              </span>
              苏格拉底式辅导
            </div>
            <p className="mt-1 text-caption leading-relaxed text-violet-700/80">
              先别急着套公式：当证据 B 出现时，哪些先验信息会影响你对 A 的判断？
            </p>
          </div>
          {/* 掌握度小徽章 */}
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-caption font-medium text-emerald-700">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-2.5 w-2.5"
                aria-hidden
              >
                <path d="M22 7 13.5 15.5 8.5 10.5 2 17M22 7h-6m6 0v6" />
              </svg>
              条件概率 掌握度 72%（+6%）
            </span>
            <span className="inline-flex items-center rounded-full bg-indigo-50 px-2 py-0.5 text-caption font-medium text-indigo-600">
              3 天后到期复习
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function Hero() {
  return (
    <header className="relative overflow-hidden">
      {/* aurora 渐变背景（TODO: 合流后换第一路 token） */}
      <div aria-hidden className="fx-aurora absolute -inset-[10%]" />
      <Particles />

      {/* 顶部导航 */}
      <nav className="relative mx-auto flex w-full max-w-6xl items-center justify-between px-5 py-4">
        <BrandMark size={32} withWordmark />
        <div className="flex items-center gap-2">
          <Link
            href="/login"
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-slate-600 transition-colors hover:bg-white/70 hover:text-slate-900"
          >
            登录
          </Link>
          <Link
            href="/register"
            className="rounded-lg bg-indigo-600 px-3.5 py-1.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-indigo-700"
          >
            注册
          </Link>
        </div>
      </nav>

      {/* 主区：文案 + 产品示意卡 */}
      <div className="relative mx-auto grid w-full max-w-6xl gap-10 px-5 pb-16 pt-10 md:grid-cols-2 md:items-center md:gap-8 md:pb-24 md:pt-16">
        <div className="fx-fade-up">
          <p className="inline-flex items-center gap-1.5 rounded-full border border-indigo-200/80 bg-white/70 px-3 py-1 text-xs font-medium text-indigo-700 backdrop-blur">
            <span className="h-1.5 w-1.5 rounded-full bg-indigo-500" />
            可验证学习者状态引擎 · VLSE
          </p>
          <h1 className="mt-4 text-4xl font-bold leading-tight tracking-tight text-slate-900 md:text-5xl">
            懂你学到哪里的
            <br />
            <span className="bg-gradient-to-r from-indigo-600 via-violet-500 to-sky-500 bg-clip-text text-transparent">
              AI 学习伙伴
            </span>
          </h1>
          <p className="mt-4 max-w-lg text-base leading-relaxed text-slate-600 md:text-lg">
            可验证的学习者状态引擎：每个回答有出处，每次进步有记录。
          </p>
          <div className="mt-7 flex flex-wrap items-center gap-3">
            <Link
              href="/register"
              className="rounded-xl bg-indigo-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-indigo-600/25 transition-all hover:-translate-y-0.5 hover:bg-indigo-700 hover:shadow-xl hover:shadow-indigo-600/30"
            >
              免费开始
            </Link>
            <Link
              href="/login"
              className="rounded-xl border border-slate-300 bg-white/80 px-6 py-3 text-sm font-semibold text-slate-700 backdrop-blur transition-all hover:-translate-y-0.5 hover:border-indigo-300 hover:text-indigo-700"
            >
              登录体验
            </Link>
          </div>
        </div>

        <div className="fx-fade-up" style={{ animationDelay: "150ms" }}>
          <ProductMock />
        </div>
      </div>
    </header>
  );
}
