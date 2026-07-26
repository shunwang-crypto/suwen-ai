"use client";

// 练习（F5）：发起表单 → SSE 流（过程时间线 + interrupt 蓝图确认）→ resume →
// 题目作答（四种控件，MathText 渲染）→ 逐题结果 + 掌握度变化；历史卷可重开。

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { AppealEntry } from "@/components/appeal-entry";
import {
  Card,
  EmptyState,
  ErrorBanner,
  MasteryDelta,
  PageHeader,
  SkeletonList,
  Spinner,
  fmtDateTime,
  fmtPct,
} from "@/components/common";
import { FlowTimeline, type FlowItem } from "@/components/flow-timeline";
import { BlueprintCard } from "@/components/quiz/blueprint-card";
import { QuizForm, type QuizFormValue } from "@/components/quiz/quiz-form";
import {
  QuestionAnswer,
  QuestionMeta,
} from "@/components/quiz/question-answer";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import { streamSse } from "@/lib/sse";
import type {
  AttemptResult,
  Blueprint,
  ErrorEventData,
  GraphDoneData,
  GraphInterruptData,
  GraphTraceData,
  KbOut,
  NodeUpdateData,
  QuizDetail,
  QuizListItem,
  QuizQuestion,
  WeakPointItem,
  WeakPointsResponse,
} from "@/lib/types";

type Phase = "idle" | "streaming" | "await_confirm" | "resuming";

/** 后端下发的逐 KC 难度分档理由（P1-1 可解释出题：定性区间词，不含掌握度数值）。 */
interface DifficultyReason {
  kc_name: string;
  zone: string;
  difficulty: string;
  reason: string;
}

/** 薄弱点预览项（/api/profile/weak-points 已附带 zone/reason 定性理由字段）。 */
type WeakPointPreview = WeakPointItem & {
  zone?: string;
  difficulty?: string;
  reason?: string;
};

// useSyncExternalStore 订阅占位：URL search 在页面生命周期内不变化（跳转即重挂载）
const emptySubscribe = () => () => {};

interface ActiveQuiz {
  id: string;
  title: string;
  questions: QuizQuestion[];
}

export default function QuizPage() {
  // URL 预填（/quiz?topic=..&kb=..，来自对话页「考考我」chip）。
  // SSR 快照为空串、客户端快照读真实 search：无水合不匹配，也无需 Suspense。
  const search = useSyncExternalStore(
    emptySubscribe,
    () => window.location.search,
    () => "",
  );
  const urlParams = new URLSearchParams(search);
  const prefillTopic = urlParams.get("topic") ?? "";
  const prefillKb = urlParams.get("kb") ?? "";
  // /quiz?weak=1（画像页/学情报告页的「针对此点练习」）：高亮薄弱点定向出题卡片
  const fromWeakLink = urlParams.get("weak") === "1";

  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [quizzes, setQuizzes] = useState<QuizListItem[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  // 画像联动出题（M4-D2）：预览与实际出题共用后端 weak_point_kcs，所见即所练
  const [weak, setWeak] = useState<WeakPointsResponse | null>(null);

  // SSE 出题流
  const [phase, setPhase] = useState<Phase>("idle");
  const [timeline, setTimeline] = useState<FlowItem[]>([]);
  const [interruptData, setInterruptData] = useState<GraphInterruptData | null>(null);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const topicRef = useRef("");
  const abortRef = useRef<AbortController | null>(null);

  // 作答
  const [quiz, setQuiz] = useState<ActiveQuiz | null>(null);
  const [quizLoading, setQuizLoading] = useState(false);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [attempt, setAttempt] = useState<AttemptResult | null>(null);

  // 首个 setState 在 await 之后（effect 内不允许同步 setState）
  const loadQuizzes = useCallback(async () => {
    try {
      const list = await apiJson<QuizListItem[]>("/api/quiz");
      setQuizzes(list);
      setListError(null);
    } catch (err) {
      setQuizzes((prev) => prev ?? []);
      setListError(err instanceof ApiError ? err.message : "历史练习加载失败");
    }
  }, []);

  // 首屏并行拉取历史练习 / 知识库 / 薄弱点预览（effect 内联 IIFE，同 chat 页模式）
  useEffect(() => {
    let alive = true;
    (async () => {
      const [quizzesRes, kbsRes, weakRes] = await Promise.allSettled([
        apiJson<QuizListItem[]>("/api/quiz"),
        apiJson<KbOut[]>("/api/kb"),
        apiJson<WeakPointsResponse>("/api/profile/weak-points"),
      ]);
      if (!alive) return;
      if (quizzesRes.status === "fulfilled") setQuizzes(quizzesRes.value);
      else {
        setQuizzes([]);
        setListError("历史练习加载失败");
      }
      setKbs(kbsRes.status === "fulfilled" ? kbsRes.value : []);
      setWeak(
        weakRes.status === "fulfilled"
          ? weakRes.value
          : { threshold: 0.6, limit: 5, weak_points: [] },
      );
    })();
    return () => {
      alive = false;
      abortRef.current?.abort();
    };
  }, []);

  /** 消费一段图执行 SSE（首次 POST /api/quiz 或 resume）。 */
  const consume = useCallback(
    async (path: string, body: unknown) => {
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        for await (const msg of streamSse(path, body, {
          signal: controller.signal,
        })) {
          switch (msg.event) {
            case "node_update":
              setTimeline((prev) => [
                ...prev,
                { kind: "node", data: msg.data as NodeUpdateData },
              ]);
              break;
            case "trace_event":
              setTimeline((prev) => [
                ...prev,
                { kind: "trace", data: msg.data as GraphTraceData },
              ]);
              break;
            case "interrupt": {
              setInterruptData(msg.data as GraphInterruptData);
              setPhase("await_confirm");
              break;
            }
            case "done": {
              const d = msg.data as GraphDoneData;
              const result = d.result ?? {};
              if (result.quiz_id && result.questions?.length) {
                setQuiz({
                  id: result.quiz_id,
                  title: topicRef.current || "本次练习",
                  questions: result.questions,
                });
                setAnswers({});
                setAttempt(null);
                setNotice(null);
              } else {
                setNotice("已取消出题（蓝图未确认），可重新发起。");
              }
              setPhase("idle");
              setInterruptData(null);
              void loadQuizzes();
              break;
            }
            case "error": {
              const d = msg.data as ErrorEventData;
              setFlowError(d.message || d.code);
              setPhase("idle");
              break;
            }
            default:
              break; // 未知事件向前兼容：忽略
          }
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;
        setFlowError(
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : "网络异常，请稍后重试",
        );
        setPhase("idle");
      } finally {
        abortRef.current = null;
        // 流意外结束（无 done/interrupt）时兜底回到空闲态
        setPhase((p) => (p === "await_confirm" ? p : "idle"));
      }
    },
    [loadQuizzes],
  );

  const startQuiz = useCallback(
    (value: QuizFormValue) => {
      topicRef.current = value.topic;
      setTimeline([]);
      setInterruptData(null);
      setFlowError(null);
      setNotice(null);
      setQuiz(null);
      setAttempt(null);
      setPhase("streaming");
      void consume("/api/quiz", value);
    },
    [consume],
  );

  /** 画像定向出题：kc_names 与难度分布都由后端按掌握度计算（M4-D2 / P1-1），
   *  前端只声明意图 —— 客户端算的难度可篡改，且与仿真验证过的 ZPD 阈值不同源。 */
  const startWeakQuiz = useCallback(() => {
    const points = weak?.weak_points ?? [];
    if (points.length === 0) return;
    topicRef.current = "薄弱点强化练习";
    setTimeline([]);
    setInterruptData(null);
    setFlowError(null);
    setNotice(null);
    setQuiz(null);
    setAttempt(null);
    setPhase("streaming");
    void consume("/api/quiz", {
      from_weak_points: true,
      num_questions: points.length,
    });
  }, [weak, consume]);

  const resume = useCallback(
    (approved: boolean, blueprint?: Blueprint) => {
      if (!interruptData) return;
      setPhase("resuming");
      void consume(interruptData.resume_url, {
        value: approved ? { approved: true, blueprint } : { approved: false },
      });
    },
    [interruptData, consume],
  );

  const openQuiz = useCallback(async (id: string) => {
    setQuizLoading(true);
    setNotice(null);
    setFlowError(null);
    try {
      const detail = await apiJson<QuizDetail>(`/api/quiz/${id}`);
      setQuiz({
        id: detail.id,
        title: detail.title || "历史练习",
        questions: detail.questions,
      });
      setAnswers({});
      setAttempt(null);
      setTimeline([]);
    } catch (err) {
      setFlowError(err instanceof ApiError ? err.message : "练习加载失败");
    } finally {
      setQuizLoading(false);
    }
  }, []);

  const submitAnswers = useCallback(async () => {
    if (!quiz || submitting) return;
    setSubmitting(true);
    setFlowError(null);
    try {
      const result = await apiJson<AttemptResult>(`/api/quiz/${quiz.id}/attempts`, {
        body: {
          answers: quiz.questions.map((q) => ({
            question_id: q.id,
            response: answers[q.id] ?? "",
          })),
        },
      });
      setAttempt(result);
      try {
        // 判分即写 BKT，薄弱点随之变化：立刻刷新预览（"上午答错，下午就在卷子上"）
        setWeak(await apiJson<WeakPointsResponse>("/api/profile/weak-points"));
      } catch {
        // 薄弱点刷新失败不影响判分结果展示
      }
    } catch (err) {
      setFlowError(err instanceof ApiError ? err.message : "提交失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }, [quiz, answers, submitting]);

  const busy = phase === "streaming" || phase === "resuming";
  // 定向出题的逐 KC 分档理由（interrupt 负载平铺，普通出题为空数组）
  const difficultyReasons =
    (interruptData?.difficulty_reasons as DifficultyReason[] | undefined) ?? [];
  const resultByQid = new Map(
    (attempt?.results ?? []).map((r) => [r.question_id, r]),
  );
  const answeredCount = quiz
    ? quiz.questions.filter((q) => (answers[q.id] ?? "").trim()).length
    : 0;

  return (
    <div className="mx-auto w-full max-w-6xl space-y-4 p-4">
      {flowError && (
        <ErrorBanner message={flowError} onClose={() => setFlowError(null)} />
      )}
      {notice && (
        <div className="flex items-center justify-between rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-700">
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="text-amber-400 hover:text-amber-600"
            aria-label="关闭提示"
          >
            ✕
          </button>
        </div>
      )}

      {quiz ? (
        /* ---- 作答 / 结果视图 ---- */
        <div className="space-y-4">
          <PageHeader
            title={quiz.title}
            subtitle={
              <>
                共 {quiz.questions.length} 题
                {attempt
                  ? ` · 答对 ${attempt.correct}/${attempt.total}`
                  : ` · 已作答 ${answeredCount}/${quiz.questions.length}`}
              </>
            }
            action={
              <button
                type="button"
                onClick={() => {
                  setQuiz(null);
                  setAttempt(null);
                }}
                className="rounded-xl border border-slate-300 bg-white px-3.5 py-1.5 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-100"
              >
                返回列表
              </button>
            }
          />

          {quiz.questions.length === 0 ? (
            <EmptyState
              title="这份练习没有题目"
              description="可能生成未完成，请重新发起"
              illustration="target"
            />
          ) : (
            <ol className="space-y-4">
              {quiz.questions.map((q, i) => {
                const result = resultByQid.get(q.id);
                return (
                  <li
                    key={q.id}
                    className="rounded-2xl border border-slate-200 bg-white p-4 shadow-[0_4px_20px_-6px_rgba(79,70,229,0.10)] transition-shadow hover:shadow-[0_6px_26px_-6px_rgba(79,70,229,0.16)]"
                  >
                    <div className="mb-2 flex items-start justify-between gap-3">
                      <p className="min-w-0 text-sm leading-relaxed text-slate-800">
                        <span className="mr-1.5 font-semibold text-indigo-600">
                          {i + 1}.
                        </span>
                        <MathText text={q.stem} />
                      </p>
                      <QuestionMeta question={q} />
                    </div>

                    <QuestionAnswer
                      question={q}
                      value={answers[q.id] ?? ""}
                      disabled={Boolean(attempt) || submitting}
                      onChange={(v) =>
                        setAnswers((prev) => ({ ...prev, [q.id]: v }))
                      }
                    />

                    {result && (
                      <div
                        className={`edu-fade-up mt-3 rounded-xl border px-3.5 py-2.5 text-sm ${
                          result.correct
                            ? "border-emerald-200 bg-emerald-50/60"
                            : "border-red-200 bg-red-50/60"
                        }`}
                        style={{ animationDelay: `${Math.min(i, 12) * 90}ms` }}
                      >
                        <p
                          className={`flex items-center gap-1.5 font-semibold ${
                            result.correct ? "text-emerald-700" : "text-red-700"
                          }`}
                        >
                          {/* 对错图标 draw 入场动画（reduced-motion 直接呈现终态） */}
                          <svg
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2.6"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            className="h-4 w-4 shrink-0"
                            aria-hidden
                          >
                            {result.correct ? (
                              <path className="edu-draw" d="M20 6 9 17l-5-5" />
                            ) : (
                              <>
                                <path className="edu-draw" d="M18 6 6 18" />
                                <path
                                  className="edu-draw"
                                  style={{ animationDelay: "0.32s" }}
                                  d="m6 6 12 12"
                                />
                              </>
                            )}
                          </svg>
                          {result.correct ? "回答正确" : "回答错误"}
                          {result.llm_score != null && (
                            <span className="rounded-full bg-white px-2 py-0.5 text-xs font-medium text-slate-500">
                              AI 评分 {(result.llm_score * 100).toFixed(0)} 分
                            </span>
                          )}
                        </p>
                        {result.scored_reason && (
                          <p className="mt-1 text-xs text-slate-600">
                            评分理由：{result.scored_reason}
                          </p>
                        )}
                        <p className="mt-1.5 text-xs text-slate-600">
                          <span className="font-medium text-slate-500">参考答案：</span>
                          <MathText text={result.answer} />
                        </p>
                        {result.explanation && (
                          <p className="mt-1 text-xs leading-relaxed text-slate-600">
                            <span className="font-medium text-slate-500">解析：</span>
                            <MathText text={result.explanation} />
                          </p>
                        )}
                        {result.mastery_changes.length > 0 && (
                          <p className="mt-2 flex flex-wrap gap-1.5">
                            {result.mastery_changes.map((c) => (
                              <MasteryDelta key={c.kc_id} change={c} />
                            ))}
                          </p>
                        )}
                        {/* 判分申诉（P2-3）：仅 LLM 评分的主观题可复核 */}
                        {q.type === "short" && result.llm_score != null && (
                          <AppealEntry questionId={q.id} />
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          )}

          {!attempt && quiz.questions.length > 0 && (
            <div className="flex justify-end">
              <button
                type="button"
                disabled={submitting || answeredCount === 0}
                onClick={() => void submitAnswers()}
                className="flex items-center gap-2 rounded-xl bg-indigo-600 px-6 py-2.5 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitting && (
                  <Spinner className="h-4 w-4 border-white/40 border-t-white" />
                )}
                {submitting ? "判分中…" : "提交答卷"}
              </button>
            </div>
          )}
        </div>
      ) : (
        /* ---- 发起 + 历史视图 ---- */
        <div className="space-y-4">
          <PageHeader
            title="智能练习"
            subtitle="从主题或知识库发起 AI 出题，蓝图确认后生成题目；判分即时更新画像"
          />
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
          <div className="min-w-0 flex-1 space-y-4">
            {/* 画像联动出题（M4-D2）：主按钮 + 将要练的 KC 预览 */}
            <section
              className={`rounded-2xl border bg-white p-4 shadow-[0_4px_20px_-6px_rgba(79,70,229,0.10)] ${
                fromWeakLink
                  ? "border-indigo-400 ring-2 ring-indigo-100"
                  : "border-slate-200"
              }`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold text-slate-800">
                    针对我的薄弱点出题
                  </h2>
                  <p className="mt-0.5 text-xs text-slate-500">
                    知识点由学情画像自动选取（掌握度
                    {weak ? ` < ${fmtPct(weak.threshold)}` : "偏低"}
                    ，近 7 天错过的优先）
                  </p>
                </div>
                <button
                  type="button"
                  disabled={busy || !weak || weak.weak_points.length === 0}
                  onClick={startWeakQuiz}
                  className="flex shrink-0 items-center gap-2 rounded-xl bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {busy && (
                    <Spinner className="h-4 w-4 border-white/40 border-t-white" />
                  )}
                  针对我的薄弱点出题
                </button>
              </div>

              {weak === null ? (
                <p className="mt-3 flex items-center gap-2 text-xs text-slate-400">
                  <Spinner className="h-3.5 w-3.5" /> 正在读取学情画像…
                </p>
              ) : weak.weak_points.length === 0 ? (
                <p className="mt-3 rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-700">
                  还没有识别到薄弱点 —— 先做一次练习积累数据，判分后画像才会给出定向出题的知识点。
                </p>
              ) : (
                <div className="mt-3">
                  <p className="mb-1.5 text-xs text-slate-500">
                    将要练的 {weak.weak_points.length} 个知识点：
                  </p>
                  <ul className="flex flex-wrap gap-1.5">
                    {weak.weak_points.map((wp) => (
                      <li
                        key={wp.kc_id}
                        className="flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-xs text-amber-800"
                        title={`掌握度 ${fmtPct(wp.p_mastery)}`}
                      >
                        <span className="max-w-40 truncate font-medium">
                          {wp.name}
                        </span>
                        <span className="font-mono text-amber-600">
                          {fmtPct(wp.p_mastery)}
                        </span>
                        {wp.recent_error && (
                          <span className="rounded-full bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">
                            近期错过
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                  {/* 为什么练这些（P1-1）：后端逐 KC 分档理由，与实际难度分布同源 */}
                  {(weak.weak_points as WeakPointPreview[]).some((wp) => wp.reason) && (
                    <ul className="mt-2 space-y-1 border-t border-slate-100 pt-2">
                      {(weak.weak_points as WeakPointPreview[])
                        .filter((wp) => wp.reason)
                        .map((wp) => (
                          <li key={wp.kc_id} className="text-xs leading-relaxed text-slate-600">
                            <span className="font-medium text-slate-700">{wp.name}</span>
                            {wp.zone && (
                              <span className="mx-1 rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-500">
                                {wp.zone}
                              </span>
                            )}
                            {wp.reason}
                          </li>
                        ))}
                    </ul>
                  )}
                </div>
              )}
            </section>

            <Card title="发起练习">
              <QuizForm
                key={`${prefillTopic}|${prefillKb}`}
                kbs={kbs}
                busy={busy}
                onSubmit={startQuiz}
                initialTopic={prefillTopic}
                initialKbId={prefillKb}
              />
            </Card>

            {(timeline.length > 0 || busy) && (
              <FlowTimeline items={timeline} streaming={busy} />
            )}

            {phase === "await_confirm" && interruptData?.blueprint && (
              <div className="space-y-2">
                {/* 为什么这套题（P1-1）：随 blueprint interrupt 下发的逐 KC 分档理由 */}
                {difficultyReasons.length > 0 && (
                  <div className="rounded-2xl border border-indigo-200 bg-indigo-50/50 p-4">
                    <h3 className="text-sm font-semibold text-slate-900">
                      为什么是这套题
                    </h3>
                    <p className="mt-0.5 text-xs text-slate-500">
                      难度由学情画像按掌握区间自动分档（可在下方蓝图中调整）
                    </p>
                    <ul className="mt-2 space-y-1">
                      {difficultyReasons.map((r) => (
                        <li
                          key={r.kc_name}
                          className="text-xs leading-relaxed text-slate-600"
                        >
                          <span className="font-medium text-slate-800">
                            {r.kc_name}
                          </span>
                          <span className="mx-1 rounded-full bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700">
                            {r.zone}
                          </span>
                          {r.reason}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                <BlueprintCard
                  key={interruptData.thread_id}
                  blueprint={interruptData.blueprint}
                  busy={false}
                  onConfirm={(bp) => resume(true, bp)}
                  onCancel={() => resume(false)}
                />
              </div>
            )}
            {phase === "resuming" && (
              <p className="flex items-center gap-2 text-sm text-slate-500">
                <Spinner /> 蓝图已提交，正在生成并校验题目…
              </p>
            )}
          </div>

          <aside className="w-full shrink-0 lg:w-80">
            <Card title="历史练习">
              {listError && (
                <div className="mb-2">
                  <ErrorBanner message={listError} onRetry={() => void loadQuizzes()} />
                </div>
              )}
              {quizzes === null ? (
                <SkeletonList rows={4} />
              ) : quizzes.length === 0 ? (
                <p className="py-4 text-center text-xs text-slate-400">
                  还没有练习记录
                </p>
              ) : (
                <ul className="space-y-1">
                  {quizzes.map((qz) => (
                    <li key={qz.id}>
                      <button
                        type="button"
                        disabled={quizLoading}
                        onClick={() => void openQuiz(qz.id)}
                        className="w-full rounded-lg px-3 py-2 text-left text-sm text-slate-700 transition-all duration-200 hover:-translate-y-0.5 hover:bg-indigo-50 hover:shadow-[0_4px_14px_-4px_rgba(79,70,229,0.20)] disabled:opacity-50 motion-reduce:hover:translate-y-0"
                      >
                        <span className="block truncate font-medium">
                          {qz.title || "未命名练习"}
                        </span>
                        <span className="block text-xs text-slate-400">
                          {fmtDateTime(qz.created_at)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          </aside>
          </div>
        </div>
      )}
    </div>
  );
}
