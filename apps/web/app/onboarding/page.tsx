"use client";

// Onboarding 三步轻表单（P2-4, G12 冷启动）：目标一句话 → 兴趣 chips → 时长与讲解偏好。
// 注册成功后跳到这里（register/page.tsx），全程可跳过（右上角 → /chat）。
// 提交 PUT /api/preferences 后双 CTA：「先做 5 题摸底」(/quiz) 或「直接开始」(/chat)。
// 讲解偏好是内容组织维度（严谨/口语/类比优先），不给学习者贴任何感官类型标签（该类分类已被证伪，全仓禁用）。

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { BrandMark } from "@/components/ui/brand";
import { ApiError, apiJson } from "@/lib/api";

// 局部类型（P1 先例：不动 apps/web/lib/types.ts）
interface PreferencesPayload {
  explanation_style: string | null;
  verbosity: string | null;
  interests: string[];
  daily_minutes: number | null;
  goal: string | null;
}

const STYLE_OPTIONS = [
  { value: "rigorous", label: "严谨", desc: "定义定理优先，推导严密" },
  { value: "colloquial", label: "口语", desc: "大白话讲解，轻松易懂" },
  { value: "analogy", label: "类比优先", desc: "先打比方，再进正题" },
];

const VERBOSITY_OPTIONS = [
  { value: "concise", label: "简洁", desc: "要点式，点到为止" },
  { value: "detailed", label: "详尽", desc: "步骤完整，例子充分" },
];

const MINUTES_OPTIONS = [15, 30, 45, 60, 90];

const PRESET_INTERESTS = [
  "篮球",
  "足球",
  "动漫",
  "游戏",
  "音乐",
  "科幻",
  "编程",
  "历史",
  "美食",
  "旅行",
];

const INTERESTS_MAX = 8; // 与后端 routers/preferences.py 同限
const INTEREST_CHAR_MAX = 20;
const GOAL_MAX = 200;

const STEPS = ["学习目标", "兴趣", "学习习惯"];

/** 可选中 chip（兴趣 / 时长 / 偏好共用的轻量样式按钮）。 */
function Chip({
  active,
  onClick,
  children,
  title,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={`rounded-full border px-3.5 py-1.5 text-sm font-medium transition-colors ${
        active
          ? "border-brand-500 bg-brand-50 text-brand-700 shadow-subtle"
          : "border-slate-300/80 bg-white/80 text-slate-600 hover:border-brand-300 hover:text-brand-600"
      }`}
    >
      {children}
    </button>
  );
}

export default function OnboardingPage() {
  const router = useRouter();
  const [step, setStep] = useState(0); // 0..2 表单步；3 = 完成页
  const [goal, setGoal] = useState("");
  const [interests, setInterests] = useState<string[]>([]);
  const [customInterest, setCustomInterest] = useState("");
  const [minutes, setMinutes] = useState<number | null>(null);
  const [style, setStyle] = useState<string | null>(null);
  const [verbosity, setVerbosity] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const toggleInterest = (tag: string) => {
    setInterests((prev) =>
      prev.includes(tag)
        ? prev.filter((t) => t !== tag)
        : prev.length >= INTERESTS_MAX
          ? prev
          : [...prev, tag],
    );
  };

  const addCustomInterest = () => {
    const tag = customInterest.replace(/\s+/g, " ").trim();
    if (!tag) return;
    if (tag.length > INTEREST_CHAR_MAX) {
      setError(`单个兴趣不超过 ${INTEREST_CHAR_MAX} 字`);
      return;
    }
    if (interests.length >= INTERESTS_MAX && !interests.includes(tag)) {
      setError(`最多选择 ${INTERESTS_MAX} 个兴趣`);
      return;
    }
    setError(null);
    if (!interests.includes(tag)) setInterests((prev) => [...prev, tag]);
    setCustomInterest("");
  };

  const submit = async () => {
    setError(null);
    setSubmitting(true);
    try {
      const body: PreferencesPayload = {
        explanation_style: style,
        verbosity,
        interests,
        daily_minutes: minutes,
        goal: goal.trim() || null,
      };
      await apiJson("/api/preferences", { method: "PUT", body });
      setStep(3); // 完成页（双 CTA）
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-dvh flex-col bg-aurora">
      {/* 顶栏：品牌 + 跳过 */}
      <header className="flex items-center justify-between px-6 py-4">
        <BrandMark size={32} withWordmark className="text-slate-900" />
        {step < 3 && (
          <Link
            href="/chat"
            className="text-sm text-slate-400 transition-colors hover:text-slate-600"
          >
            跳过
          </Link>
        )}
      </header>

      <main className="flex flex-1 items-start justify-center px-4 pb-16 pt-6 sm:items-center sm:pt-0">
        <div className="w-full max-w-xl animate-fade-up">
          {step < 3 && (
            <>
              {/* 步骤指示 */}
              <div className="mb-6 flex items-center justify-center gap-2">
                {STEPS.map((label, i) => (
                  <div key={label} className="flex items-center gap-2">
                    <span
                      className={`flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold ${
                        i < step
                          ? "bg-brand-500 text-white"
                          : i === step
                            ? "bg-brand-100 text-brand-700 ring-2 ring-brand-500/40"
                            : "bg-slate-200 text-slate-500"
                      }`}
                    >
                      {i + 1}
                    </span>
                    <span
                      className={`text-xs ${i === step ? "font-medium text-slate-700" : "text-slate-400"}`}
                    >
                      {label}
                    </span>
                    {i < STEPS.length - 1 && (
                      <span className="h-px w-6 bg-slate-300" aria-hidden />
                    )}
                  </div>
                ))}
              </div>

              <div className="glass rounded-card border border-white/70 p-6 shadow-floating ring-1 ring-slate-900/5 sm:p-8">
                {error && (
                  <div
                    role="alert"
                    className="mb-4 animate-pop rounded-control border border-danger-500/25 bg-danger-50 px-3 py-2 text-sm text-danger-700"
                  >
                    {error}
                  </div>
                )}

                {step === 0 && (
                  <section>
                    <h1 className="text-xl font-semibold tracking-tight text-slate-900">
                      这段时间最想达成什么目标？
                    </h1>
                    <p className="mt-1 text-sm text-slate-500">
                      一句话就好，AI 助教会围绕它安排讲解与练习。
                    </p>
                    <input
                      value={goal}
                      maxLength={GOAL_MAX}
                      onChange={(e) => setGoal(e.target.value)}
                      placeholder="如：高数期末冲 90 分"
                      className="mt-5 w-full rounded-control border border-slate-300/80 bg-white/90 px-3.5 py-2.5 text-sm text-slate-900 outline-none transition-[border-color,box-shadow] duration-200 placeholder:text-slate-400 focus:border-brand-500 focus:shadow-card focus:ring-4 focus:ring-brand-500/10"
                    />
                  </section>
                )}

                {step === 1 && (
                  <section>
                    <h1 className="text-xl font-semibold tracking-tight text-slate-900">
                      平时喜欢什么？
                    </h1>
                    <p className="mt-1 text-sm text-slate-500">
                      例题会自然贴合你的兴趣来举例（最多 {INTERESTS_MAX} 个，可不选）。
                    </p>
                    <div className="mt-5 flex flex-wrap gap-2">
                      {PRESET_INTERESTS.map((tag) => (
                        <Chip
                          key={tag}
                          active={interests.includes(tag)}
                          onClick={() => toggleInterest(tag)}
                        >
                          {tag}
                        </Chip>
                      ))}
                      {interests
                        .filter((t) => !PRESET_INTERESTS.includes(t))
                        .map((tag) => (
                          <Chip
                            key={tag}
                            active
                            title="点击移除"
                            onClick={() => toggleInterest(tag)}
                          >
                            {tag} ×
                          </Chip>
                        ))}
                    </div>
                    <div className="mt-4 flex gap-2">
                      <input
                        value={customInterest}
                        maxLength={INTEREST_CHAR_MAX}
                        onChange={(e) => setCustomInterest(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            addCustomInterest();
                          }
                        }}
                        placeholder="自己填一个，如：天文"
                        className="min-w-0 flex-1 rounded-control border border-slate-300/80 bg-white/90 px-3.5 py-2 text-sm text-slate-900 outline-none transition-[border-color,box-shadow] duration-200 placeholder:text-slate-400 focus:border-brand-500 focus:ring-4 focus:ring-brand-500/10"
                      />
                      <button
                        type="button"
                        onClick={addCustomInterest}
                        className="shrink-0 rounded-control border border-slate-300/80 bg-white/80 px-4 text-sm font-medium text-slate-600 transition-colors hover:border-brand-300 hover:text-brand-600"
                      >
                        添加
                      </button>
                    </div>
                  </section>
                )}

                {step === 2 && (
                  <section className="space-y-6">
                    <div>
                      <h1 className="text-xl font-semibold tracking-tight text-slate-900">
                        每天大概能学多久？
                      </h1>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {MINUTES_OPTIONS.map((m) => (
                          <Chip
                            key={m}
                            active={minutes === m}
                            onClick={() => setMinutes(minutes === m ? null : m)}
                          >
                            {m} 分钟
                          </Chip>
                        ))}
                      </div>
                    </div>
                    <div>
                      <h2 className="text-sm font-semibold text-slate-700">
                        喜欢什么样的讲解？
                      </h2>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {STYLE_OPTIONS.map((o) => (
                          <Chip
                            key={o.value}
                            title={o.desc}
                            active={style === o.value}
                            onClick={() =>
                              setStyle(style === o.value ? null : o.value)
                            }
                          >
                            {o.label}
                          </Chip>
                        ))}
                      </div>
                    </div>
                    <div>
                      <h2 className="text-sm font-semibold text-slate-700">
                        讲多细？
                      </h2>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {VERBOSITY_OPTIONS.map((o) => (
                          <Chip
                            key={o.value}
                            title={o.desc}
                            active={verbosity === o.value}
                            onClick={() =>
                              setVerbosity(
                                verbosity === o.value ? null : o.value,
                              )
                            }
                          >
                            {o.label}
                          </Chip>
                        ))}
                      </div>
                    </div>
                  </section>
                )}

                {/* 底部按钮 */}
                <div className="mt-8 flex items-center justify-between">
                  <button
                    type="button"
                    onClick={() => setStep((s) => Math.max(0, s - 1))}
                    disabled={step === 0}
                    className="rounded-control px-4 py-2 text-sm font-medium text-slate-500 transition-colors hover:bg-slate-100 disabled:invisible"
                  >
                    上一步
                  </button>
                  {step < 2 ? (
                    <button
                      type="button"
                      onClick={() => setStep((s) => s + 1)}
                      className="btn-brand rounded-control px-6 py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating"
                    >
                      下一步
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={submit}
                      disabled={submitting}
                      className="btn-brand rounded-control px-6 py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      {submitting ? "保存中…" : "完成"}
                    </button>
                  )}
                </div>
              </div>
            </>
          )}

          {step === 3 && (
            <div className="glass animate-pop rounded-card border border-white/70 p-8 text-center shadow-floating ring-1 ring-slate-900/5">
              <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-brand-50">
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-7 w-7 text-brand-600"
                  aria-hidden
                >
                  <path d="M20 6 9 17l-5-5" />
                </svg>
              </div>
              <h1 className="mt-4 text-xl font-semibold tracking-tight text-slate-900">
                偏好已保存
              </h1>
              <p className="mt-2 text-sm text-slate-500">
                做 5 题摸底能让 AI 助教更快了解你的水平，推荐先测一测。
              </p>
              <div className="mt-6 flex flex-col justify-center gap-3 sm:flex-row">
                <button
                  type="button"
                  onClick={() => router.replace("/practice/session?mode=quiz&from=%2Ftoday&topic=%E6%91%B8%E5%BA%95%E7%BB%83%E4%B9%A0")}
                  className="btn-brand rounded-control px-6 py-2.5 text-sm font-semibold text-white shadow-card hover:shadow-floating"
                >
                  先做 5 题摸底
                </button>
                <button
                  type="button"
                  onClick={() => router.replace("/chat")}
                  className="rounded-control border border-slate-300/80 bg-white/80 px-6 py-2.5 text-sm font-semibold text-slate-600 transition-colors hover:border-brand-300 hover:text-brand-600"
                >
                  直接开始
                </button>
              </div>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
