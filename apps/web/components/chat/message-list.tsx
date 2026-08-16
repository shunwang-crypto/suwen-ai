"use client";

/**
 * 聊天消息列表 (从 app/chat/page.tsx 机械拆出, 渲染逻辑零变更):
 * 空态示例问题 / 用户与助手气泡 / 流式光标 / 软超时与断流角标 /
 * 追问 chips / 回答评价 (👍/👎 + 原因) / 已参考学情画像标签。
 */

import type { RefObject } from "react";
import { useRouter } from "next/navigation";
import type { CSSProperties } from "react";

import { MathText } from "@/lib/katex";
import { ModelMarkdown } from "@/components/chat/model-markdown";
import { ProcessTimeline } from "@/components/chat/timeline";
import { MODE_LABELS, SAMPLE_QUESTIONS } from "@/components/chat/chat-msg";
import type { ChatMode, ChatMsg } from "@/components/chat/chat-msg";
import type { TimelineItem } from "@/components/chat/timeline";
import type { CitationData } from "@/lib/types";

interface MessageListProps {
  messages: ChatMsg[];
  streaming: boolean;
  scrollRef: RefObject<HTMLDivElement | null>;
  ratingPick: string | null;
  setRatingPick: (value: string | null) => void;
  submitRating: (m: ChatMsg, rating: "good" | "bad", reason: string | null) => void;
  send: (q: string) => void;
  retryFrom: (id: string) => void;
  handleCitationClick: (msgId: string, idx: number) => void;
  openMessageCitations: (msgId: string) => void;
  citedMsgId: string | null;
  activeCitation: number | null;
}

function WebSearchStatus({
mode,
streaming,
timeline,
citations,
onOpen,
}: {
mode: ChatMode;
streaming: boolean;
timeline: TimelineItem[];
citations: CitationData[];
onOpen: () => void;
}) {
if (mode !== "web" && mode !== "research") return null;
const searchEvents = timeline.filter(
  (item) => item.kind === "trace" && item.data.type === "web_search",
);
const completedRounds = searchEvents.filter(
  (item) => item.kind === "trace" && !item.data.skipped,
).length;
const deep = mode === "research";
if (streaming || citations.length === 0) return null;
const searchedTotal = searchEvents.reduce((total, item) => {
  const value = Number(item.data.total ?? item.data.returned ?? 0);
  return Number.isFinite(value) ? Math.max(total, value) : total;
}, citations.length);
const citedCount = citations.filter((citation) => citation.cited).length;
const status = `搜索 ${searchedTotal} 个网页${citedCount > 0 ? ` · 引用 ${citedCount} 个` : ""}${deep && completedRounds > 1 ? " · 2 轮搜索" : ""}`;

return (
  <button
    type="button"
    onClick={onOpen}
    className="mb-3 inline-flex items-center gap-2 rounded-full bg-surface-muted px-3 py-1.5 text-body-sm text-ink-secondary hover:bg-line hover:text-ink"
  >
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" className="h-4 w-4 shrink-0" aria-hidden>
      <circle cx="11" cy="11" r="7" />
      <path d="m16 16 4 4" />
    </svg>
    <span>{status}</span>
  </button>
);
}

/**
 * 消息流主体：渲染对话消息、过程时间线与联网搜索状态，由 ChatPage 组合使用。
 */

export function MessageList(props: MessageListProps) {
  const router = useRouter();
  const {
    messages,
    streaming,
    scrollRef,
    ratingPick,
    setRatingPick,
    submitRating,
    send,
    retryFrom,
    handleCitationClick,
    openMessageCitations,
    citedMsgId,
    activeCitation,
  } = props;
  return (
    <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-[720px] flex-col gap-5 px-6 py-6">
        {messages.length === 0 ? (
          <div className="fx-fade-up flex flex-col items-center gap-3 py-24 text-center">
            <span className="flex h-10 w-10 items-center justify-center rounded-overlay bg-brand-50 text-xl font-bold text-brand-700">
              A
            </span>
            <div>
              <h2 className="text-2xl font-bold leading-8 text-slate-900">
                今天想从哪里开始？
              </h2>
              <p className="mt-2 text-sm text-slate-500">
                可以直接提问，也可以从系统根据学习状态整理的下一步开始。
              </p>
            </div>
            <div className="mt-1 flex flex-wrap justify-center gap-2">
              {SAMPLE_QUESTIONS.map((q, i) => (
                <button
                  key={q}
                  type="button"
                  onClick={() => void send(q)}
                  style={{ "--fx-delay": `${120 + i * 90}ms` } as CSSProperties}
                  className="fx-chip-in rounded-control border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-500 transition-colors hover:border-brand-300 hover:text-brand-700"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="fx-msg-in flex justify-end">
                <div className="max-w-[85%] rounded-control bg-brand-50 px-3.5 py-2.5 text-sm leading-[22px] text-slate-900">
                  {m.attachments && m.attachments.length > 0 && (
                    <div className="mb-2 flex flex-wrap justify-end gap-1.5">
                      {m.attachments.map((attachment) => (
                        <span
                          key={attachment.id}
                          className="inline-flex max-w-56 items-center gap-1.5 rounded-[7px] border border-brand-100 bg-white/80 px-2 py-1 text-body-sm text-ink-secondary"
                          title={attachment.filename}
                        >
                          <span aria-hidden>
                            {attachment.kind === "image" ? "图片" : "文件"}
                          </span>
                          <span className="truncate">{attachment.filename}</span>
                        </span>
                      ))}
                    </div>
                  )}
                  <MathText text={m.content} />
                </div>
              </div>
            ) : (
              <div key={m.id} className="fx-msg-in flex justify-start">
                <div className="w-full">
                  {m.chatMode && (
                    <WebSearchStatus
                      mode={m.chatMode}
                      streaming={Boolean(m.streaming)}
                      timeline={m.timeline}
                      citations={m.citations.filter((citation) => citation.source_type === "web")}
                      onOpen={() => openMessageCitations(m.id)}
                    />
                  )}
                  {m.done?.refused && (
                    <div className="mb-2 flex items-center gap-2.5 rounded-xl border border-amber-200 bg-gradient-to-r from-amber-50 to-orange-50/60 px-3 py-2 text-xs text-amber-700">
                      {/* 温和插画感小图标：放大镜找不到内容的守护小盾 */}
                      <svg
                        viewBox="0 0 40 40"
                        className="h-8 w-8 shrink-0"
                        aria-hidden
                      >
                        <circle cx="17" cy="16" r="10" fill="#fef3c7" />
                        <circle
                          cx="17"
                          cy="16"
                          r="10"
                          fill="none"
                          stroke="#d97706"
                          strokeWidth="2"
                        />
                        <path
                          d="M13 16h8"
                          stroke="#d97706"
                          strokeWidth="2"
                          strokeLinecap="round"
                        />
                        <path
                          d="m24.5 23.5 7 7"
                          stroke="#d97706"
                          strokeWidth="2.5"
                          strokeLinecap="round"
                        />
                        <path
                          d="M31 9c1.5 1 3 1.2 4.5 1v4.2c0 3-1.8 4.6-4.5 5.8-2.7-1.2-4.5-2.8-4.5-5.8V10c1.5.2 3 0 4.5-1Z"
                          fill="#fde68a"
                          stroke="#d97706"
                          strokeWidth="1.6"
                          strokeLinejoin="round"
                        />
                      </svg>
                      <span>
                        已拒答：知识库中未检索到足够依据，为避免幻觉未调用模型作答。
                      </span>
                    </div>
                  )}

                  {(m.chatMode === "fast" ||
                    m.chatMode === "think" ||
                    m.streaming) && (
                    <ProcessTimeline
                      items={m.timeline}
                      streaming={Boolean(m.streaming)}
                      answerStarted={Boolean(m.content)}
                    />
                  )}

                  {(m.content || m.streaming) && (
                    <div
                      className={`text-sm leading-[1.75] text-slate-800 ${
                        m.done?.refused
                          ? "rounded-control border border-amber-200 bg-amber-50/50 px-3.5 py-3"
                          : ""
                      }`}
                    >
                      {m.content ? (
                        <>
                          <ModelMarkdown
                            text={m.content}
                            citationIndexes={m.citations.map((citation) => citation.idx)}
                            onCitationClick={(idx) =>
                              handleCitationClick(m.id, idx)
                            }
                            activeCitation={
                              citedMsgId === m.id ? activeCitation : null
                            }
                          />
                          {/* token 流式光标：闪烁 + 流光尾迹（纯视觉） */}
                          {m.streaming && (
                            <span className="fx-caret" aria-hidden />
                          )}
                        </>
                      ) : m.timeline.length === 0 ? (
                        <span className="inline-flex items-center gap-2 text-slate-400">
                          {/* 首 token 前的「正在思考」三点跳动指示器 */}
                          <span className="fx-typing" aria-hidden>
                            <span />
                            <span />
                            <span />
                          </span>
                          正在思考…
                        </span>
                      ) : null}
                    </div>
                  )}

                  {/* 软超时提示（P2-18）：45s 无事件仍在等待 */}
                  {m.streaming && m.stalled && (
                    <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-700">
                      服务响应较慢：已较长时间未收到新内容，仍在等待…可点「停止」后重试。
                    </div>
                  )}

                  {/* 断流角标（P0-6）：服务端已保存半截答案，可一键重问 */}
                  {!m.streaming && m.truncated && (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <span className="inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-body-sm font-medium text-amber-700">
                        <svg
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          className="h-3 w-3"
                          aria-hidden
                        >
                          <path d="M12 8v4m0 4h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z" />
                        </svg>
                        回答未完成
                      </span>
                      <button
                        type="button"
                        disabled={streaming}
                        onClick={() => retryFrom(m.id)}
                        className="rounded-full border border-indigo-200 bg-indigo-50/60 px-3 py-1 text-xs text-indigo-700 transition-colors hover:border-indigo-300 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        重新提问
                      </button>
                    </div>
                  )}

                  {/* 追问 chips + 固定「考考我」chip（kb 路径回答完成后） */}
                  {!m.streaming &&
                    m.done &&
                    !m.errorText &&
                    ((m.followups?.length ?? 0) > 0 || m.kbId) && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {(m.followups ?? []).map((q, qi) => (
                          <button
                            key={q}
                            type="button"
                            disabled={streaming}
                            onClick={() => void send(q)}
                            style={
                              {
                                "--fx-delay": `${qi * 80}ms`,
                              } as CSSProperties
                            }
                            className="fx-chip-in rounded-control border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-600 transition-colors hover:border-brand-300 hover:text-brand-700 disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            <MathText text={q} />
                          </button>
                        ))}
                        {m.kbId && (
                          <button
                            type="button"
                            onClick={() =>
                              router.push(
                                `/quiz?topic=${encodeURIComponent(m.kbName ?? "")}&kb=${encodeURIComponent(m.kbId ?? "")}`,
                              )
                            }
                            style={
                              {
                                "--fx-delay": `${(m.followups?.length ?? 0) * 80}ms`,
                              } as CSSProperties
                            }
                            className="fx-chip-in flex items-center gap-1 rounded-control border border-brand-200 bg-brand-50 px-2.5 py-1.5 text-xs font-medium text-brand-700 transition-colors hover:bg-brand-100"
                          >
                            <svg
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="2"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              className="h-3 w-3"
                              aria-hidden
                            >
                              <path d="M12 2 15 8.5 22 9.3 17 14l1.2 7L12 17.8 5.8 21 7 14 2 9.3 9 8.5Z" />
                            </svg>
                            考考我
                          </button>
                        )}
                      </div>
                    )}

                  {m.interrupt && (
                    <div className="mt-2 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs text-indigo-700">
                      <p className="font-medium">
                        工作流已暂停，等待确认（{m.interrupt.kind}）
                      </p>
                      <p className="mt-0.5 break-all font-mono text-indigo-400">
                        {m.interrupt.resume_url}
                      </p>
                    </div>
                  )}

                  {m.errorText && (
                    <div className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                      生成出错：{m.errorText}
                    </div>
                  )}

                  {!m.streaming && m.done && !m.errorText && (
                    <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                      <span
                        className="h-2 w-2 shrink-0 rounded-full bg-sky-500"
                        aria-hidden
                      />
                      <span>
                        回答完成
                        {m.done.usage?.latency_ms != null &&
                          ` · ${(m.done.usage.latency_ms / 1000).toFixed(1)} 秒`}
                        {m.done.usage?.mode &&
                          ` · ${MODE_LABELS[m.done.usage.mode] ?? m.done.usage.mode}`}
                      </span>
                      {m.citations.length > 0 &&
                        !m.citations.some((citation) => citation.source_type === "web") && (
                          <button
                            type="button"
                            onClick={() => openMessageCitations(m.id)}
                            className="rounded-full px-2 py-0.5 text-ink-muted hover:bg-surface-muted hover:text-ink-secondary"
                          >
                            {m.citations.length} 条知识库引用
                          </button>
                        )}
                      {m.timeline.some(
                        (item) => item.kind === "trace" && item.data?.type === "profile",
                      ) && (
                        <span
                          className="rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-violet-600"
                          title="本次回答参考了你的学情摘要（偏好与薄弱点），可在设置 → AI 记住的你 中管理"
                        >
                          已参考学情画像
                        </span>
                      )}
                      <span className="mx-1 h-3 w-px bg-slate-200" aria-hidden />
                      <button
                        type="button"
                        aria-label="讲得好"
                        aria-pressed={m.rating === "good"}
                        onClick={() => void submitRating(m, "good", null)}
                        className={`rounded-full px-2 py-0.5 transition-colors ${
                          m.rating === "good"
                            ? "bg-emerald-50 text-emerald-600"
                            : "text-slate-400 hover:bg-surface-muted hover:text-ink-secondary"
                        }`}
                      >
                        👍 讲得好
                      </button>
                      <button
                        type="button"
                        aria-label="讲得不好"
                        aria-pressed={m.rating === "bad"}
                        onClick={() =>
                          m.rating === "bad"
                            ? setRatingPick(ratingPick === m.id ? null : m.id)
                            : setRatingPick(m.id)
                        }
                        className={`rounded-full px-2 py-0.5 transition-colors ${
                          m.rating === "bad"
                            ? "bg-amber-50 text-amber-600"
                            : "text-slate-400 hover:bg-surface-muted hover:text-ink-secondary"
                        }`}
                      >
                        👎 讲得不好
                      </button>
                    </div>
                  )}

                  {ratingPick === m.id && !m.streaming && (
                    <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                      <span className="text-ink-muted">哪里不好：</span>
                      {[
                        { value: "too_long", label: "太啰嗦" },
                        { value: "too_short", label: "太简略" },
                        { value: "confusing", label: "没听懂" },
                        { value: "off_topic", label: "不是我想问的" },
                      ].map((option) => (
                        <button
                          key={option.value}
                          type="button"
                          onClick={() => {
                            setRatingPick(null);
                            void submitRating(m, "bad", option.value);
                          }}
                          className={`rounded-full border px-2.5 py-1 transition-colors ${
                            m.ratingReason === option.value
                              ? "border-amber-300 bg-amber-50 text-amber-700"
                              : "border-slate-200 bg-white text-slate-600 hover:border-amber-200 hover:text-amber-700"
                          }`}
                        >
                          {option.label}
                        </button>
                      ))}
                      <button
                        type="button"
                        onClick={() => {
                          setRatingPick(null);
                          void submitRating(m, "bad", null);
                        }}
                        className="rounded-full px-2 py-1 text-ink-muted hover:text-ink-secondary"
                      >
                        直接标记不佳
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ),
          )
        )}
      </div>
    </div>
  );
}
