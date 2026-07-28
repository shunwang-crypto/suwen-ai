"use client";

// 苏格拉底辅导（F4）：左侧题目输入（数学公式实时预览）+ 提示阶梯状态；右侧多轮对话。
// 提示层级徽章跟随每轮回复；「要个提示」发送固定话术；solved → 复盘卡
// （误解 + 复习建议 + 掌握度变化）；换题重置 thread_local_id 开新会话。

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import {
  Card,
  EmptyState,
  ErrorBanner,
  FxStyles,
  MasteryDelta,
  Spinner,
} from "@/components/common";
import { WorkspaceNav } from "@/components/workspace-nav";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import type {
  MasteryChange,
  TutorSessionSummary,
  TutorTurnResult,
} from "@/lib/types";

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
const FINISH_PHRASE = "我已经理解了，这道题解决了，谢谢老师！";

interface TutorMessage {
  role: "user" | "assistant";
  content: string;
  /** assistant 消息记录该轮的提示层级 */
  hintLevel?: number;
}

/** 提示层级徽章（第 N 层 · 名称）。 */
function HintBadge({ level }: { level: number }) {
  const idx = Math.min(Math.max(level, 0), 3);
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${HINT_BADGE_STYLES[idx]}`}
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
              className={`z-10 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold transition-all duration-300 ${
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
              className={`flex min-w-0 flex-1 items-center rounded-lg px-2 py-1 text-xs transition-colors duration-300 ${
                current
                  ? "bg-indigo-50 font-semibold text-indigo-700"
                  : reached
                    ? "text-slate-600"
                    : "text-slate-400"
              }`}
            >
              {label}
              {i === 3 && (
                <span className="ml-auto text-[10px] text-slate-400">仅此层给答案</span>
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
        <p className="relative text-[11px] font-semibold uppercase tracking-widest text-emerald-100">
          本次收获
        </p>
        <div className="relative mt-0.5 flex items-center justify-between gap-2">
          <h3 className="text-sm font-semibold text-white">本次辅导复盘</h3>
          <span className="rounded-full bg-white/20 px-2 py-0.5 text-xs font-medium text-white ring-1 ring-inset ring-white/40">
            已解决
          </span>
        </div>
      </div>

      <div className="space-y-3 p-4 text-sm">
        <div>
          <p className="mb-1 text-xs font-semibold text-slate-500">
            本次暴露的误解
          </p>
          {summary.misconceptions.length === 0 ? (
            <p className="text-xs text-slate-400">本次未发现明显误解</p>
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
          <p className="mb-1 text-xs font-semibold text-slate-500">复习建议</p>
          {summary.review_suggestions.length === 0 ? (
            <p className="text-xs text-slate-400">暂无</p>
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
            <p className="mb-1 text-xs font-semibold text-slate-500">
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
  const [problem, setProblem] = useState("");
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<TutorMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hintLevel, setHintLevel] = useState(0);
  const [stuckType, setStuckType] = useState("");
  const [summary, setSummary] = useState<TutorSessionSummary | null>(null);
  const [masteryChanges, setMasteryChanges] = useState<MasteryChange[]>([]);

  const started = messages.length > 0;
  const ended = summary !== null;
  const listRef = useRef<HTMLDivElement>(null);

  // 新消息自动滚到底部
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, sending, summary]);

  const send = async (text: string) => {
    const message = text.trim();
    const prob = problem.trim();
    if (!message || !prob || sending || ended) return;
    setSending(true);
    setError(null);
    setMessages((prev) => [...prev, { role: "user", content: message }]);
    try {
      const res = await apiJson<TutorTurnResult>("/api/tutor", {
        body: {
          problem: prob,
          message,
          thread_local_id: threadId ?? undefined,
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
        // summarize 契约: {misconceptions, review_suggestions}; 解析失败按空小结兜底
        let parsed: TutorSessionSummary = {
          misconceptions: [],
          review_suggestions: [],
        };
        try {
          const raw = JSON.parse(res.session_summary) as Partial<TutorSessionSummary>;
          parsed = {
            misconceptions: Array.isArray(raw.misconceptions)
              ? raw.misconceptions
              : [],
            review_suggestions: Array.isArray(raw.review_suggestions)
              ? raw.review_suggestions
              : [],
          };
        } catch {
          // 保留空小结
        }
        setSummary(parsed);
        setMasteryChanges(res.mastery_changes ?? []);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "辅导请求失败，请重试");
      // 回滚本轮未送达的学生消息，避免与服务端 thread 状态不一致
      setMessages((prev) => prev.slice(0, -1));
    } finally {
      setSending(false);
    }
  };

  const submit = () => {
    const text = input;
    setInput("");
    void send(text);
  };

  // 换题：重置 thread_local_id 与全部会话状态
  const reset = () => {
    setThreadId(null);
    setMessages([]);
    setInput("");
    setError(null);
    setHintLevel(0);
    setStuckType("");
    setSummary(null);
    setMasteryChanges([]);
  };

  return (
    <div className="mx-auto w-full max-w-6xl space-y-4 p-4">
      <WorkspaceNav workspace="practice" />
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
      {/* 左：题目 + 提示阶梯 */}
      <aside className="w-full shrink-0 space-y-4 lg:w-80">
        <Card
          title="题目"
          extra={
            started && (
              <button
                type="button"
                onClick={reset}
                className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-600 hover:border-indigo-300 hover:text-indigo-700"
              >
                换一道题
              </button>
            )
          }
        >
          <textarea
            value={problem}
            onChange={(e) => setProblem(e.target.value)}
            disabled={started}
            rows={5}
            maxLength={4000}
            placeholder={"粘贴题干，支持数学公式：行内 $x^2$，块级 $$\\int_0^1 x\\,dx$$"}
            className="w-full resize-y rounded-xl border border-slate-300 px-3 py-2 text-sm text-slate-900 outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-50 disabled:text-slate-500"
          />
          {problem.trim() && (
            <div className="mt-2 rounded-xl bg-slate-50 p-3">
              <p className="mb-1 text-xs text-slate-400">预览</p>
              <MathText
                text={problem}
                className="text-sm leading-relaxed text-slate-800"
              />
            </div>
          )}
          {started && (
            <p className="mt-2 text-xs text-slate-400">
              会话进行中题目已锁定，点「换一道题」开始新会话
            </p>
          )}
        </Card>

        <Card
          title="提示阶梯"
          extra={
            started &&
            stuckType && (
              <span className="text-xs text-slate-400">
                卡点：{STUCK_LABELS[stuckType] ?? stuckType}
              </span>
            )
          }
        >
          <HintLadder level={hintLevel} active={started} />
          <p className="mt-2 text-xs leading-relaxed text-slate-400">
            老师不会直接给答案：先引导你说出思路，逐层给提示；索要答案会被温和坚持一次，第
            3 层才展开完整解答。
          </p>
        </Card>
      </aside>

      {/* 右：对话区 */}
      <div className="flex min-h-[70dvh] min-w-0 flex-1 flex-col rounded-2xl border border-slate-200 bg-white shadow-[0_4px_20px_-6px_rgba(79,70,229,0.10)]">
        <header className="flex items-center justify-between gap-2 border-b border-slate-100 px-4 py-2.5">
          <h2 className="text-sm font-semibold text-slate-800">苏格拉底辅导</h2>
          {started && <HintBadge level={hintLevel} />}
        </header>

        <div ref={listRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
          {error && <ErrorBanner message={error} onClose={() => setError(null)} />}

          {!started ? (
            <EmptyState
              title="从一道让你卡住的题开始"
              description="左侧粘贴题目，然后告诉老师你的思路或者卡在哪一步"
              illustration="chat"
            />
          ) : (
            messages.map((m, i) =>
              m.role === "user" ? (
                <div key={i} className="edu-fade-up flex justify-end">
                  <div className="max-w-[85%] rounded-2xl rounded-br-md bg-indigo-600 px-3.5 py-2 text-sm text-white">
                    <MathText text={m.content} />
                  </div>
                </div>
              ) : (
                <div key={i} className="edu-fade-up flex flex-col items-start gap-1">
                  <div className="max-w-[85%] rounded-2xl rounded-bl-md border border-slate-200 bg-slate-50 px-3.5 py-2 text-sm text-slate-800">
                    <MathText text={m.content} />
                  </div>
                  {m.hintLevel != null && <HintBadge level={m.hintLevel} />}
                </div>
              ),
            )
          )}

          {sending && (
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <Spinner className="h-3.5 w-3.5" />
              老师思考中…
            </div>
          )}

          {/* 第 3 层已给完整解答但尚未确认解决：引导生成复盘 */}
          {!ended && !sending && hintLevel === 3 && (
            <div className="rounded-2xl border border-amber-200 bg-amber-50/60 p-3 text-sm">
              <p className="text-amber-800">
                已给出完整解答。理解之后告诉老师，生成本次辅导复盘卡。
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => void send(FINISH_PHRASE)}
                  className="rounded-xl bg-amber-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-amber-700"
                >
                  我理解了，生成复盘
                </button>
                <button
                  type="button"
                  onClick={reset}
                  className="rounded-xl border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-700 hover:bg-amber-100"
                >
                  换一道题
                </button>
              </div>
            </div>
          )}

          {ended && summary && (
            <ReviewCard
              summary={summary}
              changes={masteryChanges}
              onReset={reset}
            />
          )}
        </div>

        {/* 输入区 */}
        <footer className="border-t border-slate-100 p-3">
          {ended ? (
            <p className="py-1 text-center text-xs text-slate-400">
              本次辅导已结束，点复盘卡里的「换一道题」继续
            </p>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                submit();
              }}
              className="flex items-end gap-2"
            >
              <button
                type="button"
                disabled={!problem.trim() || sending}
                onClick={() => void send(HINT_PHRASE)}
                title={`发送固定话术「${HINT_PHRASE}」`}
                className="flex shrink-0 items-center gap-1 rounded-xl border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm font-medium text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-4 w-4"
                  aria-hidden
                >
                  <path d="M9 18h6m-5 3h4M12 3a6 6 0 0 0-3.5 10.9c.7.5 1.1 1.3 1.1 2.1h4.8c0-.8.4-1.6 1.1-2.1A6 6 0 0 0 12 3Z" />
                </svg>
                要个提示
              </button>
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                maxLength={4000}
                placeholder={
                  problem.trim()
                    ? started
                      ? "回应老师的问题，或说出你的下一步想法…"
                      : "先说说你的思路，或者卡在哪一步…"
                    : "请先在左侧输入题目"
                }
                disabled={!problem.trim() || sending}
                className="min-w-0 flex-1 rounded-xl border border-slate-300 px-3.5 py-2 text-sm text-slate-900 outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-50"
              />
              <button
                type="submit"
                disabled={!problem.trim() || !input.trim() || sending}
                className="flex shrink-0 items-center gap-1.5 rounded-control bg-ink px-4 py-2 text-sm font-semibold text-white hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
              >
                {sending && (
                  <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />
                )}
                发送
              </button>
            </form>
          )}
        </footer>
      </div>
      </div>
    </div>
  );
}
