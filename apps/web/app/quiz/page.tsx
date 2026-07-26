"use client";

// 练习（F5）：发起表单 → SSE 流（过程时间线 + interrupt 蓝图确认）→ resume →
// 题目作答（四种控件，MathText 渲染）→ 逐题结果 + 掌握度变化；历史卷可重开。

import { useCallback, useEffect, useRef, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  LoadingState,
  MasteryDelta,
  Spinner,
  fmtDateTime,
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
} from "@/lib/types";

type Phase = "idle" | "streaming" | "await_confirm" | "resuming";

interface ActiveQuiz {
  id: string;
  title: string;
  questions: QuizQuestion[];
}

export default function QuizPage() {
  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [quizzes, setQuizzes] = useState<QuizListItem[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);

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

  // 首屏并行拉取历史练习与知识库列表（effect 内联 IIFE，同 chat 页模式）
  useEffect(() => {
    let alive = true;
    (async () => {
      const [quizzesRes, kbsRes] = await Promise.allSettled([
        apiJson<QuizListItem[]>("/api/quiz"),
        apiJson<KbOut[]>("/api/kb"),
      ]);
      if (!alive) return;
      if (quizzesRes.status === "fulfilled") setQuizzes(quizzesRes.value);
      else {
        setQuizzes([]);
        setListError("历史练习加载失败");
      }
      setKbs(kbsRes.status === "fulfilled" ? kbsRes.value : []);
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
    } catch (err) {
      setFlowError(err instanceof ApiError ? err.message : "提交失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }, [quiz, answers, submitting]);

  const busy = phase === "streaming" || phase === "resuming";
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
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-base font-semibold text-slate-900">
                {quiz.title}
              </h2>
              <p className="text-xs text-slate-400">
                共 {quiz.questions.length} 题
                {attempt
                  ? ` · 答对 ${attempt.correct}/${attempt.total}`
                  : ` · 已作答 ${answeredCount}/${quiz.questions.length}`}
              </p>
            </div>
            <button
              type="button"
              onClick={() => {
                setQuiz(null);
                setAttempt(null);
              }}
              className="rounded-xl border border-slate-300 bg-white px-3.5 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-100"
            >
              返回列表
            </button>
          </div>

          {quiz.questions.length === 0 ? (
            <EmptyState title="这份练习没有题目" description="可能生成未完成，请重新发起" />
          ) : (
            <ol className="space-y-4">
              {quiz.questions.map((q, i) => {
                const result = resultByQid.get(q.id);
                return (
                  <li
                    key={q.id}
                    className="rounded-2xl border border-slate-200 bg-white p-4"
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
                        className={`mt-3 rounded-xl border px-3.5 py-2.5 text-sm ${
                          result.correct
                            ? "border-emerald-200 bg-emerald-50/60"
                            : "border-red-200 bg-red-50/60"
                        }`}
                      >
                        <p
                          className={`flex items-center gap-1.5 font-semibold ${
                            result.correct ? "text-emerald-700" : "text-red-700"
                          }`}
                        >
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
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
          <div className="min-w-0 flex-1 space-y-4">
            <Card title="发起练习">
              <QuizForm kbs={kbs} busy={busy} onSubmit={startQuiz} />
            </Card>

            {(timeline.length > 0 || busy) && (
              <FlowTimeline items={timeline} streaming={busy} />
            )}

            {phase === "await_confirm" && interruptData?.blueprint && (
              <BlueprintCard
                key={interruptData.thread_id}
                blueprint={interruptData.blueprint}
                busy={false}
                onConfirm={(bp) => resume(true, bp)}
                onCancel={() => resume(false)}
              />
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
                <LoadingState text="历史练习加载中…" />
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
                        className="w-full rounded-lg px-3 py-2 text-left text-sm text-slate-700 hover:bg-indigo-50 disabled:opacity-50"
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
      )}
    </div>
  );
}
