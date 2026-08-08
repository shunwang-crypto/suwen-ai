"use client";

// 今日复习（F6）：到期错题队列卡片 → 作答 → 对错反馈 + next_due + 状态机变化
//（mastered 庆祝样式）；变式重练按钮换同考点新题；空队列友好空态。
// 卡片附 FSRS 遗忘快照（P2-7「再不复习，3 天后预计保持率 N%」，<90% 才显示）。

import Link from "next/link";
import { useEffect, useState, useSyncExternalStore } from "react";

import { AppealEntry } from "@/components/appeal-entry";
import {
  Card,
  EmptyState,
  ErrorBanner,
  MasteryDelta,
  PageHeader,
  Skeleton,
  SkeletonList,
  Spinner,
  fmtDateTime,
  fmtRelative,
} from "@/components/common";
import { QuestionAnswer, QuestionMeta } from "@/components/quiz/question-answer";
import { Celebrate } from "@/components/ui/celebrate";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type {
  ReviewAnswerResult,
  ReviewItem,
  ReviewTodayResponse,
  ReviewVariantResult,
} from "@/lib/types";

/** 错因五分类中文化（models.py: concept_confusion/formula_error/calc_slip/misread/knowledge_gap）。 */
const CAUSE_LABELS: Record<string, string> = {
  concept_confusion: "概念混淆",
  formula_error: "公式用错",
  calc_slip: "计算失误",
  misread: "审题偏差",
  knowledge_gap: "知识缺口",
};

/** 错因五色语义化徽章样式（与错题本同一套色）。 */
const CAUSE_BADGE_STYLES: Record<string, string> = {
  concept_confusion: "bg-brand-50 text-brand-700 ring-brand-100",
  formula_error: "bg-warning-50 text-warning-700 ring-warning-500/20",
  calc_slip: "bg-warning-50 text-warning-700 ring-warning-500/20",
  misread: "bg-warning-50 text-warning-700 ring-warning-500/20",
  knowledge_gap: "bg-danger-50 text-danger-700 ring-danger-500/20",
};
const CAUSE_BADGE_FALLBACK = "bg-slate-100 text-slate-600 ring-slate-200/70";

const STATE_LABELS: Record<string, { label: string; className: string }> = {
  new: { label: "新错题", className: "bg-red-50 text-red-600" },
  reviewing: { label: "复习中", className: "bg-amber-50 text-amber-700" },
  mastered: { label: "已掌握", className: "bg-emerald-50 text-emerald-700" },
};

/** 遗忘快照只在 3 天后保持率低于此值时渲染（P2-7，与画像页同口径）。 */
const RETENTION_SHOW_BELOW = 0.9;
const emptySubscribe = () => () => {};

/** 队列项 + 遗忘快照字段（P2-7，后端已附带；共享 types.ts 不改，页面局部扩展）。 */
type ReviewItemWithRetention = ReviewItem & {
  retention_now?: number | null;
  retention_in_3d?: number | null;
};

function StateBadge({ state }: { state: string }) {
  const meta = STATE_LABELS[state] ?? {
    label: state,
    className: "bg-slate-100 text-slate-600",
  };
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-xs font-medium ${meta.className}`}
    >
      {meta.label}
    </span>
  );
}

interface ItemState {
  value: string;
  submitting: boolean;
  variantLoading: boolean;
  isVariant: boolean;
  result: ReviewAnswerResult | null;
}

const initialItemState: ItemState = {
  value: "",
  submitting: false,
  variantLoading: false,
  isVariant: false,
  result: null,
};

export default function ReviewsPage() {
  const search = useSyncExternalStore(
    emptySubscribe,
    () => window.location.search,
    () => "",
  );
  const urlParams = new URLSearchParams(search);
  const planId = urlParams.get("plan_id") ?? "";
  const taskKey = urlParams.get("task_key") ?? "";
  const [data, setData] = useState<ReviewTodayResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [states, setStates] = useState<Record<string, ItemState>>({});
  const [currentIndex, setCurrentIndex] = useState(0);
  const [showExitConfirm, setShowExitConfirm] = useState(false);
  const [completed, setCompleted] = useState(false);
  const [openExplanation, setOpenExplanation] = useState<Record<string, boolean>>({});

  const [reloadKey, setReloadKey] = useState(0);

  // 队列加载：reloadKey 自增触发（effect 内联 IIFE，同 chat 页模式）
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<ReviewTodayResponse>("/api/reviews/today");
        if (!alive) return;
        setData(res);
        setStates({});
        setCurrentIndex(0);
        setCompleted(false);
        setError(null);
      } catch (err) {
        if (alive) {
          setError(err instanceof ApiError ? err.message : "复习队列加载失败");
        }
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [reloadKey]);

  const reload = () => {
    setLoading(true);
    setError(null);
    setReloadKey((k) => k + 1);
  };

  const patchState = (id: string, patch: Partial<ItemState>) => {
    setStates((prev) => ({
      ...prev,
      [id]: { ...(prev[id] ?? initialItemState), ...patch },
    }));
  };

  const submit = async (item: ReviewItem) => {
    const st = states[item.error_item_id] ?? initialItemState;
    if (st.submitting || st.result) return;
    patchState(item.error_item_id, { submitting: true });
    setBanner(null);
    try {
      const result = await apiJson<ReviewAnswerResult>(
        `/api/reviews/${item.error_item_id}/answer`,
        {
          body: {
            response: st.value,
            ...(planId && taskKey ? { plan_id: planId, task_key: taskKey } : {}),
          },
        },
      );
      patchState(item.error_item_id, { submitting: false, result });
    } catch (err) {
      patchState(item.error_item_id, { submitting: false });
      setBanner(err instanceof ApiError ? err.message : "提交失败，请重试");
    }
  };

  const makeVariant = async (item: ReviewItem) => {
    const st = states[item.error_item_id] ?? initialItemState;
    if (st.variantLoading) return;
    patchState(item.error_item_id, { variantLoading: true });
    setBanner(null);
    try {
      const res = await apiJson<ReviewVariantResult>(
        `/api/reviews/${item.error_item_id}/variant`,
        { method: "POST", body: {} },
      );
      // 新题插入：替换该卡片的题目并重置作答态
      setData((prev) =>
        prev
          ? {
              ...prev,
              items: prev.items.map((it) =>
                it.error_item_id === item.error_item_id
                  ? { ...it, question: res.question }
                  : it,
              ),
            }
          : prev,
      );
      setStates((prev) => ({
        ...prev,
        [item.error_item_id]: { ...initialItemState, isVariant: true },
      }));
    } catch (err) {
      patchState(item.error_item_id, { variantLoading: false });
      setBanner(err instanceof ApiError ? err.message : "变式生成失败，请重试");
    }
  };

  const finishedCount = Object.values(states).filter((state) => state.result).length;
  const correctCount = Object.values(states).filter((state) => state.result?.correct).length;

  return (
    <div className="mx-auto w-full max-w-[980px] space-y-4 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      <PageHeader
        title="今日复习"
        subtitle={
          data
            ? `业务时间 ${fmtDateTime(data.now)} · 今日 ${data.items.length} 题${
                // B3: 总到期数与开场卡同源（后端 total_due），此前两页各显示一个数字互相打架
                (data.total_due ?? 0) > data.items.length
                  ? ` · 还有 ${(data.total_due ?? 0) - data.items.length} 题排队中`
                  : ""
              }`
            : "按遗忘曲线安排的到期错题，答对两次即攻克"
        }
        action={
          <button
            type="button"
            onClick={reload}
            className="rounded-[8px] border border-line-strong bg-white px-3.5 py-1.5 text-[12px] font-medium text-ink-secondary transition-colors hover:border-brand-200 hover:text-brand-700"
          >
            刷新队列
          </button>
        }
      />

      {banner && <ErrorBanner message={banner} onClose={() => setBanner(null)} />}
      {error && <ErrorBanner message={error} onRetry={reload} />}

      {loading ? (
        <div className="space-y-4">
          {[0, 1].map((i) => (
            <div
              key={i}
              className="rounded-2xl border border-slate-200 bg-white p-4"
            >
              <div className="mb-3 flex gap-2">
                <Skeleton className="h-5 w-14 rounded-full" />
                <Skeleton className="h-5 w-20 rounded-full" />
              </div>
              <Skeleton className="mb-3 h-4 w-4/5" />
              <SkeletonList rows={2} />
            </div>
          ))}
          <span className="sr-only">复习队列加载中…</span>
        </div>
      ) : completed && data ? (
        <section className="rounded-[14px] border border-line bg-white px-5 py-10 text-center sm:px-8">
          <span className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-success-50 text-xl text-success-600">✓</span>
          <h2 className="mt-4 text-[18px] font-semibold text-ink">今日复习已完成</h2>
          <p className="mt-2 text-[12px] text-ink-muted">
            完成 {finishedCount} 题，答对 {correctCount} 题；记忆进度和学习画像已按真实作答更新。
          </p>
          <div className="mt-6 flex flex-wrap justify-center gap-2">
            <Link href="/today" className="rounded-[8px] bg-brand-600 px-4 py-2 text-[12px] font-medium text-white">返回今日</Link>
            <Link href="/errors" className="rounded-[8px] border border-line-strong px-4 py-2 text-[12px] font-medium text-ink-secondary">查看错题本</Link>
            {data.items.find((item) => states[item.error_item_id]?.result?.correct === false) && (
              <Link href={`/tutor?problem=${encodeURIComponent(data.items.find((item) => states[item.error_item_id]?.result?.correct === false)?.question.stem ?? "")}`} className="rounded-[8px] border border-brand-200 bg-brand-50 px-4 py-2 text-[12px] font-medium text-brand-700">辅导未掌握题目</Link>
            )}
          </div>
        </section>
      ) : !data || data.items.length === 0 ? (
        <EmptyState
          title="今日无到期复习"
          description="错题会按遗忘曲线安排复习；答错的题也会自动进入错题本"
          illustration="calendar"
        />
      ) : (
        <>
        <div className="flex flex-wrap items-center justify-between gap-3 py-1 text-[11px]"><p><strong className="text-ink">第 {currentIndex + 1} 题</strong><span className="ml-3 text-ink-muted">今日 {data.items.length} 题 · 已完成 {finishedCount} · 剩余 {Math.max(0, data.items.length - finishedCount)}</span></p><button type="button" onClick={() => setShowExitConfirm(true)} className="text-ink-secondary hover:text-brand-700">← 暂时退出</button></div>
        <div className="h-1.5 overflow-hidden rounded-full bg-line" aria-label={`复习进度 ${currentIndex + 1}/${data.items.length}`}><div className="h-full rounded-full bg-brand-600 transition-[width]" style={{ width: `${((currentIndex + 1) / data.items.length) * 100}%` }} /></div>
        <ul className="space-y-4">
          {(data.items as ReviewItemWithRetention[]).slice(currentIndex, currentIndex + 1).map((item) => {
            const st = states[item.error_item_id] ?? initialItemState;
            const mastered = st.result?.state === "mastered";
            const wrong = Boolean(st.result && !st.result.correct);
            return (
              <li key={item.error_item_id}>
                <Card
                  className={`relative ${
                    mastered
                      ? "ring-2 ring-emerald-300 transition-shadow duration-500"
                      : wrong
                        ? "edu-shake"
                        : !st.result
                          ? "border-l-4 border-l-amber-400"
                          : ""
                  }`}
                >
                  {/* mastered 庆祝彩带（reduced-motion 降级为静态徽章） */}
                  {mastered && <Celebrate />}
                  <div className="mb-2 flex flex-wrap items-center gap-1.5">
                    <StateBadge state={st.result?.state ?? item.state} />
                    {(() => {
                      const cause = st.result?.cause ?? item.cause;
                      return (
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
                            CAUSE_BADGE_STYLES[cause] ?? CAUSE_BADGE_FALLBACK
                          }`}
                        >
                          {/* 答错后错因由后端自动重新分类，以最新结果为准 */}
                          错因：{CAUSE_LABELS[cause] ?? cause}
                        </span>
                      );
                    })()}
                    {item.due && (
                      <span
                        className="text-xs text-slate-400"
                        title={fmtDateTime(item.due)}
                      >
                        到期 {fmtRelative(item.due)}
                      </span>
                    )}
                    {/* 遗忘快照（P2-7）：作答后卡片已被 FSRS 重排，预测随之过期故隐藏 */}
                    {!st.result &&
                      item.retention_in_3d != null &&
                      item.retention_in_3d < RETENTION_SHOW_BELOW && (
                        <span
                          className="rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700"
                          title={
                            item.retention_now != null
                              ? `当前预计保持率 ${Math.floor(item.retention_now * 100)}%（FSRS 遗忘曲线确定性预测）`
                              : undefined
                          }
                        >
                          再不复习，3 天后预计保持率{" "}
                          {Math.floor(item.retention_in_3d * 100)}%
                        </span>
                      )}
                    {st.isVariant && (
                      <span className="rounded-full bg-violet-50 px-2 py-0.5 text-xs font-medium text-violet-600">
                        变式题
                      </span>
                    )}
                    <span className="flex-1" />
                    <QuestionMeta question={item.question} />
                  </div>

                  <p className="mb-2.5 text-sm leading-relaxed text-slate-800">
                    <MathText text={item.question.stem} />
                  </p>

                  <QuestionAnswer
                    question={item.question}
                    value={st.value}
                    disabled={Boolean(st.result) || st.submitting}
                    onChange={(v) => patchState(item.error_item_id, { value: v })}
                  />

                  {st.result ? (
                    <div
                      className={`mt-3 rounded-xl border px-3.5 py-2.5 text-sm transition-colors duration-500 ${
                        mastered
                          ? "border-emerald-300 bg-emerald-50"
                          : st.result.correct
                            ? "border-emerald-200 bg-emerald-50/60"
                            : "border-red-200 bg-red-50/60"
                      }`}
                    >
                      {mastered ? (
                        <p className="flex flex-wrap items-center gap-2 font-semibold text-emerald-700">
                          <span className="flex h-6 w-6 animate-bounce items-center justify-center rounded-full bg-emerald-500 text-white motion-reduce:animate-none">
                            <svg
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="2.5"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              className="h-3.5 w-3.5"
                              aria-hidden
                            >
                              <path d="M20 6 9 17l-5-5" />
                            </svg>
                          </span>
                          <span className="edu-pop inline-flex animate-bounce items-center gap-1 rounded-full bg-emerald-600 px-2.5 py-0.5 text-xs font-bold text-white shadow-sm motion-reduce:animate-none">
                            已攻克
                          </span>
                          连续答对，这道题已掌握！它将移出复习队列。
                        </p>
                      ) : (
                        <p
                          className={`font-semibold ${
                            st.result.correct ? "text-emerald-700" : "text-red-700"
                          }`}
                        >
                          {st.result.correct
                            ? "回答正确，继续保持！"
                            : "回答错误，连对计数已清零。"}
                        </p>
                      )}
                      <p className="mt-1 text-xs text-slate-600">
                        下次复习：
                        <span
                          className="font-medium"
                          title={fmtDateTime(st.result.next_due)}
                        >
                          {fmtRelative(st.result.next_due)}
                        </span>
                        <span className="ml-1 text-slate-400">
                          （{fmtDateTime(st.result.next_due)}）
                        </span>
                        · 状态：
                        {STATE_LABELS[st.result.state]?.label ?? st.result.state}
                      </p>
                      <div className="mt-3 rounded-[8px] border border-line bg-white/70">
                        <button type="button" onClick={() => setOpenExplanation((current) => ({ ...current, [item.error_item_id]: !current[item.error_item_id] }))} className="flex w-full items-center justify-between px-3 py-2 text-left text-[11px] font-medium text-ink-secondary">
                          <span>查看解析与思路</span>
                          <span aria-hidden>{openExplanation[item.error_item_id] ? "⌃" : "⌄"}</span>
                        </button>
                        {openExplanation[item.error_item_id] && (
                          <div className="space-y-2 border-t border-line px-3 py-3 text-xs leading-5 text-slate-600">
                            <p><span className="font-medium text-slate-500">参考答案：</span><MathText text={st.result.answer} /></p>
                            {st.result.explanation && <p><span className="font-medium text-slate-500">解析：</span><MathText text={st.result.explanation} /></p>}
                          </div>
                        )}
                      </div>
                      {st.result.mastery_changes.length > 0 && (
                        <p className="mt-2 flex flex-wrap gap-1.5">
                          {st.result.mastery_changes.map((c) => (
                            <MasteryDelta key={c.kc_id} change={c} />
                          ))}
                        </p>
                      )}
                      {/* 判分申诉（P2-3）：主观题行入口；复习作答本身不写 attempts，
                          申诉对象是该题最近一次测评的 LLM 判分，后端按 question_id 定位 */}
                      {item.question.type === "short" && (
                        <AppealEntry questionId={item.question.id} />
                      )}
                      <div className="mt-3 flex flex-wrap justify-end gap-2"><Link href={`/tutor?problem=${encodeURIComponent(item.question.stem)}`} className="rounded-[8px] border border-brand-200 bg-white px-4 py-2 text-[12px] font-medium text-brand-700">带题去辅导</Link><button type="button" onClick={() => currentIndex < data.items.length - 1 ? setCurrentIndex((index) => index + 1) : setCompleted(true)} className="rounded-[8px] bg-brand-600 px-4 py-2 text-[12px] font-medium text-white hover:bg-brand-800">{currentIndex < data.items.length - 1 ? "继续下一题 →" : "完成今日复习"}</button></div>
                    </div>
                  ) : (
                    <div className="mt-3 flex items-center justify-end gap-2">
                      <button
                        type="button"
                        disabled={st.variantLoading || st.submitting}
                        onClick={() => void makeVariant(item)}
                        className="flex items-center gap-1.5 rounded-xl border border-violet-200 bg-violet-50 px-3.5 py-2 text-sm font-medium text-violet-700 hover:bg-violet-100 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {st.variantLoading && <Spinner className="h-3.5 w-3.5" />}
                        {st.variantLoading ? "生成变式中…" : "变式重练"}
                      </button>
                      <button
                        type="button"
                        disabled={st.submitting || !st.value.trim()}
                        onClick={() => void submit(item)}
                        className="flex items-center gap-1.5 rounded-control bg-ink px-4 py-2 text-sm font-semibold text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {st.submitting && (
                          <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />
                        )}
                        {st.submitting ? "判分中…" : "提交答案"}
                      </button>
                    </div>
                  )}
                </Card>
              </li>
            );
          })}
        </ul>
        </>
      )}
      {showExitConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <button type="button" aria-label="关闭退出确认" className="absolute inset-0 bg-[#1c1917]/35" onClick={() => setShowExitConfirm(false)} />
          <section role="dialog" aria-modal="true" aria-labelledby="review-exit-title" className="relative w-full max-w-[400px] rounded-[14px] border border-line bg-white p-5 shadow-floating">
            <h2 id="review-exit-title" className="text-[16px] font-semibold text-ink">暂时退出复习？</h2>
            <p className="mt-2 text-[12px] leading-5 text-ink-muted">已提交的答案和记忆进度会保留，未完成题目仍留在复习队列。</p>
            <div className="mt-5 flex justify-end gap-2"><button type="button" onClick={() => setShowExitConfirm(false)} className="rounded-[8px] border border-line-strong px-4 py-2 text-[12px] font-medium text-ink-secondary">继续复习</button><Link href="/today" className="rounded-[8px] bg-brand-600 px-4 py-2 text-[12px] font-medium text-white">确认退出</Link></div>
          </section>
        </div>
      )}
    </div>
  );
}
