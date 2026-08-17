"use client";

// 苏格拉底辅导（F4）：左侧题目输入（数学公式实时预览）+ 提示阶梯状态；右侧多轮对话。
// 提示层级徽章跟随每轮回复；「要个提示」发送固定话术；solved → 复盘卡
// （误解 + 复习建议 + 掌握度变化）；换题重置 thread_local_id 开新会话。

import Link from "next/link";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import {
  ErrorBanner,
  FxStyles,
  MasteryDelta,
  Spinner,
} from "@/components/common";
import { ModelMarkdown } from "@/components/chat/model-markdown";
import { TutorEntrypoints } from "@/components/tutor/tutor-entrypoints";
import { TutorProblemInput } from "@/components/tutor/tutor-problem-input";
import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { MathText } from "@/lib/katex";
import type {
  KbOut,
  MasteryChange,
  TutorSessionState,
  TutorSessionSummary,
  TutorProblemDraft,
  TutorContextOut,
  TutorEntrypointsOut,
  TutorTurnResult,
} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";

const emptySubscribe = () => () => {};
const TUTOR_SESSION_KEY = "suwen:active-tutor-session";

const HINT_LEVELS = ["引导提问", "方向提示", "关键步骤", "完整解答"];

const HINT_BADGE_STYLES = [
  "bg-slate-100 text-slate-600",
  "bg-indigo-50 text-indigo-700",
  "bg-amber-50 text-amber-700",
  "bg-red-50 text-red-700",
];

const STUCK_LABELS: Record<string, string> = {
  concept: "概念理解",
  calculation: "计算过程",
  misread: "审题偏差",
  unknown: "待定位",
};

const HINT_PHRASE = "请给我一点提示";
const START_PHRASE = "我还没有明确思路，请引导我开始解题";
const FINISH_PHRASE = "我已经理解了，这道题解决了，谢谢老师！";

interface TutorMessage {
  role: "user" | "assistant";
  content: string;
  /** assistant 消息记录该轮的提示层级 */
  hintLevel?: number;
}

interface SavedTutorSession {
  draftId: string;
  threadId?: string;
}

export function tutorSessionKey(userId: string): string {
  return `${TUTOR_SESSION_KEY}:${userId}`;
}

export function readTutorSession(userId: string): SavedTutorSession | null {
  try {
    const key = tutorSessionKey(userId);
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<SavedTutorSession>;
    if (!value.draftId) {
      window.localStorage.removeItem(key);
      return null;
    }
    return value as SavedTutorSession;
  } catch {
    window.localStorage.removeItem(tutorSessionKey(userId));
    return null;
  }
}

export function writeTutorSession(userId: string, value: SavedTutorSession): void {
  window.localStorage.setItem(tutorSessionKey(userId), JSON.stringify(value));
}

function parseTutorSummary(raw: string): TutorSessionSummary | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<TutorSessionSummary>;
    return {
      misconceptions: Array.isArray(value.misconceptions) ? value.misconceptions : [],
      review_suggestions: Array.isArray(value.review_suggestions)
        ? value.review_suggestions
        : [],
    };
  } catch {
    return { misconceptions: [], review_suggestions: [] };
  }
}

/** 提示层级徽章（第 N 层 · 名称）。 */
function HintBadge({ level }: { level: number }) {
  const idx = Math.min(Math.max(level, 0), 3);
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-caption font-medium ${HINT_BADGE_STYLES[idx]}`}
    >
      第 {idx} 层 · {HINT_LEVELS[idx]}
    </span>
  );
}

/** 提示阶梯（Khanmigo 式）：垂直步骤条 —— 已过层打勾、当前层放大主色、未达层置灰。 */
function HintLadder({ level, active }: { level: number; active: boolean }) {
  return (
    <ol>
      {HINT_LEVELS.map((label, i) => {
        const reached = active && i <= level;
        const current = active && i === level;
        const passed = active && i < level;
        return (
          <li key={label} className="relative flex items-center gap-2.5 pb-3 last:pb-0">
            {/* 连接线：已走过的段落染主色 */}
            {i < HINT_LEVELS.length - 1 && (
              <span
                aria-hidden
                className={`absolute left-[11px] top-7 h-[calc(100%-1.5rem)] w-0.5 rounded-full transition-colors duration-300 ${
                  passed ? "bg-indigo-300" : "bg-slate-200"
                }`}
              />
            )}
            <span
              className={`z-10 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-body-sm font-semibold transition-all duration-300 ${
                current
                  ? "scale-110 bg-indigo-600 text-white shadow-[0_2px_10px_-2px_rgba(79,70,229,0.6)] ring-4 ring-indigo-100 motion-reduce:scale-100"
                  : passed
                    ? "bg-indigo-100 text-indigo-600"
                    : "bg-slate-100 text-slate-400"
              }`}
            >
              {passed ? (
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="3"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-3 w-3"
                  aria-label="已经过"
                >
                  <path d="M20 6 9 17l-5-5" />
                </svg>
              ) : (
                i
              )}
            </span>
            <span
              className={`flex min-w-0 flex-1 items-center rounded-lg px-2 py-1 text-caption transition-colors duration-300 ${
                current
                  ? "bg-indigo-50 font-semibold text-indigo-700"
                  : reached
                    ? "text-slate-600"
                    : "text-slate-400"
              }`}
            >
              {label}
              {i === 3 && (
                <span className="ml-auto text-caption text-slate-400">仅此层给答案</span>
              )}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** 复盘卡：本次暴露的误解 + 复习建议 + 掌握度变化（若有）。 */
function ReviewCard({
  summary,
  changes,
  onReset,
}: {
  summary: TutorSessionSummary;
  changes: MasteryChange[];
  onReset: () => void;
}) {
  return (
    <div className="edu-fade-up overflow-hidden rounded-2xl border border-emerald-200 bg-white shadow-[0_6px_24px_-8px_rgba(16,185,129,0.35)]">
      <FxStyles />
      {/* 「本次收获」渐变头图区：纯 CSS 渐变 + 内联 SVG 装饰 */}
      <div
        className="relative px-4 pb-3 pt-4"
        style={{
          background:
            "linear-gradient(120deg, #059669 0%, #10b981 45%, #2dd4bf 100%)",
        }}
      >
        <svg
          viewBox="0 0 320 80"
          preserveAspectRatio="xMaxYMid slice"
          className="pointer-events-none absolute inset-0 h-full w-full opacity-25"
          aria-hidden
        >
          <circle cx="270" cy="8" r="34" fill="#fff" opacity=".5" />
          <circle cx="308" cy="58" r="22" fill="#fff" opacity=".35" />
          <path d="M20 66 26 54l6 12-6-4-6 4Z" fill="#fff" opacity=".6" />
          <path
            d="M245 62l3-8 3 8 8 3-8 3-3 8-3-8-8-3 8-3Z"
            fill="#fff"
            opacity=".8"
          />
          <path
            d="M60 20l2-6 2 6 6 2-6 2-2 6-2-6-6-2 6-2Z"
            fill="#fff"
            opacity=".7"
          />
        </svg>
        <p className="relative text-body-sm font-semibold uppercase tracking-widest text-emerald-100">
          本次收获
        </p>
        <div className="relative mt-0.5 flex items-center justify-between gap-2">
          <h3 className="text-sm font-semibold text-white">本次辅导复盘</h3>
          <span className="rounded-full bg-white/20 px-2 py-0.5 text-caption font-medium text-white ring-1 ring-inset ring-white/40">
            已解决
          </span>
        </div>
      </div>

      <div className="space-y-3 p-4 text-sm">
        <div>
          <p className="mb-1 text-caption font-semibold text-slate-500">
            本次暴露的误解
          </p>
          {summary.misconceptions.length === 0 ? (
            <p className="text-caption text-slate-400">本次未发现明显误解</p>
          ) : (
            <ul className="space-y-1">
              {summary.misconceptions.map((m, i) => (
                <li key={i} className="flex items-start gap-1.5 text-slate-700">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" />
                  <MathText text={m} className="text-sm" />
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <p className="mb-1 text-caption font-semibold text-slate-500">复习建议</p>
          {summary.review_suggestions.length === 0 ? (
            <p className="text-caption text-slate-400">暂无</p>
          ) : (
            <ul className="space-y-1">
              {summary.review_suggestions.map((s, i) => (
                <li key={i} className="flex items-start gap-1.5 text-slate-700">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-indigo-500" />
                  <MathText text={s} className="text-sm" />
                </li>
              ))}
            </ul>
          )}
        </div>

        {changes.length > 0 && (
          <div>
            <p className="mb-1 text-caption font-semibold text-slate-500">
              掌握度变化（辅导小结已写入画像）
            </p>
            <div className="flex flex-wrap gap-1.5">
              {changes.map((c) => (
                <MasteryDelta key={c.kc_id} change={c} label={c.name} />
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex flex-wrap gap-2 px-4 pb-4">
        <button
          type="button"
          onClick={onReset}
          className="rounded-control bg-ink px-3.5 py-2 text-sm font-semibold text-white transition-colors hover:bg-black"
        >
          换一道题
        </button>
        <Link
          href="/reviews"
          className="rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-sm font-medium text-slate-600 hover:border-indigo-300 hover:text-indigo-700"
        >
          去复习错题
        </Link>
      </div>
    </div>
  );
}

export default function TutorPage() {
  const { user } = useAuth();
  const userId = user?.id ?? "anonymous";
  const search = useSyncExternalStore(
    emptySubscribe,
    () => window.location.search,
    () => "",
  );
  const urlParams = new URLSearchParams(search);
  const prefilledProblem = urlParams.get("problem") ?? "";
  const errorId = urlParams.get("error_id") ?? "";
  const sourceKbId = urlParams.get("kb") ?? "";
  const requestedDraftId = urlParams.get("draft") ?? "";
  const planId = urlParams.get("plan_id") ?? "";
  const taskId = urlParams.get("task_id") ?? "";
  const [problem, setProblem] = useState("");
  const [problemTouched, setProblemTouched] = useState(false);
  const [sourceKb, setSourceKb] = useState<KbOut | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [draftId, setDraftId] = useState<string | null>(null);
  const [inputVersion, setInputVersion] = useState(0);
  const [messages, setMessages] = useState<TutorMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hintLevel, setHintLevel] = useState(0);
  const [stuckType, setStuckType] = useState("");
  const [summary, setSummary] = useState<TutorSessionSummary | null>(null);
  const [masteryChanges, setMasteryChanges] = useState<MasteryChange[]>([]);
  const [hintsExpanded, setHintsExpanded] = useState(false);
  const [entryChoice, setEntryChoice] = useState<"new" | null>(null);

  const started = messages.length > 0;
  const ended = summary !== null;
  const effectiveProblem = problemTouched ? problem : prefilledProblem;
  const hasProblemContext = Boolean(effectiveProblem.trim());
  const listRef = useRef<HTMLDivElement>(null);

  // A5 收敛: 初始 GET 收敛为 useApiResource (过期防护由 request-epoch 承担);
  // 错题/任务上下文加载后经同步 effect 落回页面状态
  const entrypointsRes = useApiResource<TutorEntrypointsOut>(
    prefilledProblem || errorId || planId || taskId || requestedDraftId
      ? null
      : "/api/tutor/entrypoints",
  );
  const kbListRes = useApiResource<KbOut[]>(sourceKbId ? "/api/kb" : null);
  const errorCtxRes = useApiResource<TutorContextOut>(
    errorId ? `/api/errors/${errorId}/tutor-context` : null,
    { errorMessage: "错题加载失败" },
  );
  const taskCtxRes = useApiResource<TutorContextOut>(
    planId && taskId && !hasProblemContext
      ? `/api/plans/${encodeURIComponent(planId)}/tasks/${encodeURIComponent(taskId)}/tutor-context`
      : null,
    { errorMessage: "今日任务加载失败" },
  );

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 题目/知识库/错误的单向同步, 无纯派生替代 */
  useEffect(() => {
    setSourceKb(kbListRes.data?.find((item) => item.id === sourceKbId) ?? null);
  }, [kbListRes.data, sourceKbId]);

  useEffect(() => {
    const context = errorCtxRes.data;
    if (!context) return;
    setProblem(context.problem);
    setProblemTouched(true);
    if (context.kc_ids?.length) setSourceKb(null);
  }, [errorCtxRes.data]);

  useEffect(() => {
    const context = taskCtxRes.data;
    if (!context) return;
    setProblem(context.problem);
    setProblemTouched(true);
  }, [taskCtxRes.data]);

  // 上下文加载失败统一走页面唯一的 error 展示通道 (可关闭)
  useEffect(() => {
    if (errorCtxRes.error) setError(errorCtxRes.error);
  }, [errorCtxRes.error]);
  useEffect(() => {
    if (taskCtxRes.error) setError(taskCtxRes.error);
  }, [taskCtxRes.error]);
  /* eslint-enable react-hooks/set-state-in-effect */

  useEffect(() => {
    if (!requestedDraftId) return;
    let alive = true;
    const timer = window.setTimeout(() => {
      if (!alive) return;
      setDraftId(requestedDraftId);
      setThreadId(null);
      setMessages([]);
      setSummary(null);
      setProblem("");
      setProblemTouched(false);
      setError(null);
      setInputVersion((value) => value + 1);
      void apiJson<TutorProblemDraft>(`/api/tutor/drafts/${requestedDraftId}`)
        .then((draft) => {
          if (!alive) return;
          const draftProblem = draft.confirmed_problem_text || draft.stem || "";
          if (draftProblem) {
            setProblem(draftProblem);
            setProblemTouched(true);
          }
          if (draft.thread_local_id) setThreadId(draft.thread_local_id);
        })
        .catch((caught) => {
          if (alive) setError(caught instanceof ApiError ? caught.message : "辅导附件加载失败");
        });
    }, 0);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [requestedDraftId]);

  useEffect(() => {
    const saved = readTutorSession(userId);
    if (requestedDraftId || !saved || prefilledProblem) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    if (!saved.threadId) {
      timer = setTimeout(() => {
        if (alive) setDraftId(saved.draftId);
      }, 0);
      return () => {
        alive = false;
        if (timer) clearTimeout(timer);
      };
    }
    let attempts = 0;

    const restore = async () => {
      attempts += 1;
      try {
        const state = await apiJson<TutorSessionState>(`/api/tutor/${saved.threadId}`);
        if (!alive) return;
        setProblem(state.problem);
        setProblemTouched(true);
        setThreadId(saved.threadId ?? null);
        setMessages(
          state.messages.map((message) => ({
            role: message.role,
            content: message.content,
            hintLevel: message.hint_level,
          })),
        );
        setHintLevel(state.hint_level);
        setStuckType(state.stuck_type);
        setSummary(parseTutorSummary(state.session_summary));
        setSending(state.pending);
        if (state.pending && attempts < 60) {
          timer = setTimeout(() => void restore(), 1500);
        } else if (!state.pending) {
          writeTutorSession(userId, { threadId: saved.threadId, draftId: saved.draftId });
        }
      } catch (err) {
        if (!alive) return;
        if (err instanceof ApiError && err.status === 404 && attempts < 60) {
          setThreadId(saved.threadId ?? null);
          setSending(true);
          timer = setTimeout(() => void restore(), 1500);
          return;
        }
        setSending(false);
        setError(err instanceof ApiError ? err.message : "辅导现场恢复失败");
      }
    };

    timer = setTimeout(() => {
      if (!alive) return;
      setDraftId(saved.draftId);
      void restore();
    }, 0);
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [prefilledProblem, requestedDraftId, userId]);

  // 新消息自动滚到底部
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, sending, summary]);

  const send = async (text: string, confirmedDraft?: TutorProblemDraft) => {
    const message = text.trim();
    const prob = (confirmedDraft?.confirmed_problem_text ?? effectiveProblem).trim();
    if (!message || !prob || sending || ended) return;
    const activeThreadId = confirmedDraft?.thread_local_id ?? threadId;
    const activeDraftId = confirmedDraft?.id ?? draftId;
    if (!activeThreadId || !activeDraftId) return;
    if (!threadId) setThreadId(activeThreadId);
    setDraftId(activeDraftId);
    writeTutorSession(userId, { threadId: activeThreadId, draftId: activeDraftId });
    setSending(true);
    setError(null);
    setMessages((prev) => [...prev, { role: "user", content: message }]);
    try {
      const res = await apiJson<TutorTurnResult>("/api/tutor", {
        body: {
          problem: prob,
          message,
          thread_local_id: activeThreadId,
          tutor_draft_id: activeDraftId,
          ...(planId && taskId ? { plan_id: planId, task_id: taskId } : {}),
        },
      });
      setThreadId(res.thread_local_id);
      setHintLevel(res.hint_level);
      setStuckType(res.stuck_type);
      if (res.reply) {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: res.reply, hintLevel: res.hint_level },
        ]);
      }
      if (res.session_summary) {
        setSummary(parseTutorSummary(res.session_summary));
        setMasteryChanges(res.mastery_changes ?? []);
      }
      writeTutorSession(userId, { threadId: activeThreadId, draftId: activeDraftId });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "辅导请求失败，请重试");
      // 回滚本轮未送达的学生消息，避免与服务端 thread 状态不一致
      setMessages((prev) => prev.slice(0, -1));
      writeTutorSession(userId, { threadId: activeThreadId, draftId: activeDraftId });
    } finally {
      setSending(false);
    }
  };

  const submit = () => {
    const text = input.trim();
    if (started && !text) return;
    setInput("");
    void send(text || START_PHRASE);
  };

  // 换题：重置 thread_local_id 与全部会话状态
  const reset = () => {
    window.localStorage.removeItem(tutorSessionKey(userId));
    setThreadId(null);
    setDraftId(null);
    setInputVersion((value) => value + 1);
    setProblem("");
    setProblemTouched(false);
    setMessages([]);
    setInput("");
    setError(null);
    setHintLevel(0);
    setHintsExpanded(false);
    setStuckType("");
    setSummary(null);
    setMasteryChanges([]);
    setEntryChoice(null);
  };

  return (
    <div className="mx-auto w-full max-w-[1080px] space-y-5 px-5 py-5 sm:px-7 lg:px-8 lg:py-6">
      <section className="relative overflow-hidden rounded-[12px] border border-line bg-white px-5 py-4">
        <span className="absolute inset-y-0 left-0 w-[3px] bg-brand-600" aria-hidden />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-caption"><span className="font-semibold text-brand-700">题目</span><span className="rounded-full bg-danger-50 px-1.5 py-0.5 font-medium text-danger-600">必填</span><span className="text-ink-muted">根据题目建立本次辅导上下文</span></div>
          {(started || (draftId && threadId)) && <button type="button" onClick={reset} className="rounded-[7px] border border-line px-2.5 py-1 text-body-sm text-ink-secondary hover:border-brand-200 hover:text-brand-700">换题</button>}
        </div>
        {sourceKbId && (
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-[8px] border border-brand-100 bg-brand-50 px-3 py-2 text-caption text-brand-700">
            <span>
              来自知识库：{sourceKb?.name ?? "正在确认资料范围…"}。分级辅导围绕具体题目逐层引导，不会把资料库名称当成题目内容。
            </span>
            <Link href={`/chat?new=1&kb=${encodeURIComponent(sourceKbId)}`} className="font-semibold hover:underline">
              改为基于资料提问 →
            </Link>
          </div>
        )}
        {!started && !draftId && !hasProblemContext && !entryChoice && (
          <TutorEntrypoints
            data={entrypointsRes.data}
            loading={entrypointsRes.loading}
            onChooseNew={() => setEntryChoice("new")}
          />
        )}
        {(hasProblemContext || entryChoice === "new" || draftId) && (
          <TutorProblemInput
            key={inputVersion}
            initialProblem={effectiveProblem}
            initialDraftId={draftId}
            locked={Boolean(draftId && threadId)}
            onDraftChange={(nextDraftId) => {
              setDraftId(nextDraftId);
              if (nextDraftId) writeTutorSession(userId, { draftId: nextDraftId, ...(threadId ? { threadId } : {}) });
              else window.localStorage.removeItem(tutorSessionKey(userId));
            }}
            onConfirmed={(confirmed) => {
              setProblem(confirmed.confirmed_problem_text);
              setProblemTouched(true);
              setDraftId(confirmed.id);
              setThreadId(confirmed.thread_local_id);
              void send(START_PHRASE, confirmed);
            }}
          />
        )}
        <p className="mt-2 text-caption text-ink-muted">确认后题目与附件锁定；换题会建立新的辅导对话。</p>
      </section>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_266px] lg:items-start">
        <main ref={listRef} className="min-w-0 space-y-3">
          {error && <ErrorBanner message={error} onClose={() => setError(null)} />}
          {messages.map((message, index) => message.role === "user" ? (
            <article key={index} className="ml-10 overflow-hidden rounded-[10px] border border-line bg-white">
              <div className="px-4 py-3"><p className="text-caption font-medium text-ink-muted">你的第 {Math.floor(index / 2) + 1} 步</p><div className="mt-2 text-[13px] leading-6 text-ink"><MathText text={message.content} /></div></div>
              {stuckType && index === messages.length - 2 && <div className="border-t border-warning-100 bg-warning-50 px-4 py-2.5"><div className="flex items-center gap-2 text-caption"><span className="font-semibold text-warning-700">↩ 建议调整</span><span className="rounded-full bg-brand-50 px-2 py-0.5 text-brand-700">卡点 · {STUCK_LABELS[stuckType] ?? stuckType}</span></div></div>}
            </article>
          ) : (
            <div key={index} className="flex items-start gap-3 py-2">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] bg-brand-600 text-body-sm font-semibold text-white">E</span>
              <div className="min-w-0 flex-1"><div className="flex items-center gap-2"><p className="text-caption font-semibold uppercase tracking-[0.05em] text-brand-700">AI 老师</p>{message.hintLevel != null && <HintBadge level={message.hintLevel} />}</div><div className="mt-2 rounded-[10px] border border-[#c7cef5] bg-[#f6f7ff] px-4 py-3 text-[13px] leading-6 text-[#1e1b4b]"><ModelMarkdown text={message.content} /></div></div>
            </div>
          ))}

          {sending && <div className="ml-10 flex items-center gap-2 py-2 text-body-sm text-ink-muted"><Spinner className="h-3.5 w-3.5" />老师正在整理下一步提示…</div>}

          {!ended && !sending && hintLevel === 3 && started && (
            <div className="ml-10 rounded-[10px] border border-warning-200 bg-warning-50 p-3 text-body-sm text-warning-700"><p>完整解答已经展开。确认理解后，系统会把本次真实卡点写入复盘。</p><div className="mt-2 flex gap-2"><button type="button" onClick={() => void send(FINISH_PHRASE)} className="rounded-[7px] bg-brand-600 px-3 py-1.5 font-medium text-white">我已理解，生成复盘</button><button type="button" onClick={reset} className="rounded-[7px] border border-line-strong bg-white px-3 py-1.5">换一道题</button></div></div>
          )}

          {ended && summary && <ReviewCard summary={summary} changes={masteryChanges} onReset={reset} />}

          {!ended && Boolean(draftId && threadId) && (
            <form onSubmit={(e) => { e.preventDefault(); submit(); }} className="ml-0 rounded-[12px] border border-line bg-white p-3 shadow-subtle sm:ml-10">
              <div className="mb-2 flex items-center gap-1.5 px-1 text-caption"><span className="font-semibold text-ink-secondary">你的思路</span>{!started && <span className="text-ink-muted">选填</span>}</div>
              <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); submit(); } }} rows={3} maxLength={4000} disabled={!effectiveProblem.trim() || sending} placeholder={effectiveProblem.trim() ? started ? "写下你的下一步判断、计算或疑问……" : "可以先写下思路或卡点，也可以直接开始解题……" : "请先在上方输入题目"} className="w-full resize-none border-0 bg-transparent px-1 text-[13px] leading-6 text-ink outline-none placeholder:text-ink-muted disabled:cursor-not-allowed" />
              <div className="mt-2 flex items-center gap-2 border-t border-line pt-2.5"><span className="hidden text-body-sm text-ink-muted sm:block">Ctrl / ⌘ + Enter {started ? "继续" : "开始"}</span><span className="flex-1" /><button type="button" disabled={!effectiveProblem.trim() || sending} onClick={() => void send(HINT_PHRASE)} className="rounded-[7px] border border-brand-100 bg-brand-50 px-3 py-2 text-body-sm font-medium text-brand-700 disabled:opacity-40">需要提示</button><button type="submit" disabled={!effectiveProblem.trim() || (started && !input.trim()) || sending} className="flex items-center gap-1.5 rounded-[7px] bg-brand-600 px-4 py-2 text-body-sm font-medium text-white disabled:opacity-40">{sending && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}{started ? "继续解题" : "开始解题"}</button></div>
            </form>
          )}
        </main>

        {started && <aside className="rounded-[12px] border border-line bg-white p-4 lg:sticky lg:top-5">
          <div className="flex items-center justify-between"><h2 className="text-body-sm font-semibold text-ink">提示支持</h2><span className="text-caption text-ink-muted">卡了再用</span></div>
          <button type="button" aria-expanded={hintsExpanded} onClick={() => setHintsExpanded((value) => !value)} className="mt-4 flex w-full items-center justify-between rounded-[8px] border border-brand-100 bg-brand-50 px-3 py-2 text-left text-body-sm font-medium text-brand-700">
            <span>当前提示：第 {hintLevel + 1} 层 · {HINT_LEVELS[Math.min(hintLevel, HINT_LEVELS.length - 1)]}</span>
            <span aria-hidden>{hintsExpanded ? "收起" : "展开"}</span>
          </button>
          {hintsExpanded && <div className="mt-3"><HintLadder level={hintLevel} active={started} /></div>}
          <button type="button" disabled={!effectiveProblem.trim() || sending || ended} onClick={() => void send(HINT_PHRASE)} className="mt-4 w-full rounded-[7px] border border-brand-100 bg-brand-50 px-3 py-2 text-body-sm font-medium text-brand-700 disabled:opacity-40">需要更具体的提示：关键步骤</button>
          {hintLevel === 3 && !ended && <button type="button" onClick={() => void send(FINISH_PHRASE)} className="mt-2 w-full rounded-[7px] border border-success-200 bg-success-50 px-3 py-2 text-body-sm font-medium text-success-700">我认为已解出，进入理解确认</button>}
          <p className="mt-3 text-caption leading-5 text-ink-muted">提示会根据你的回答逐层加深，尽量帮助你自己找到下一步，而不是直接给出答案。</p>
        </aside>}
      </div>
    </div>
  );
}
