"use client";

// 主对话界面：会话列表 / 流式消息（token 增量 + 过程时间线）/ 引用面板。
// SSE 走 fetch+ReadableStream（lib/sse.ts），支持契约七类事件。

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { CitationsPanel } from "@/components/chat/citations-panel";
import { SessionList } from "@/components/chat/session-list";
import { ProcessTimeline, type TimelineItem } from "@/components/chat/timeline";
import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import { streamSse } from "@/lib/sse";
import type {
  CitationData,
  DoneData,
  ErrorEventData,
  InterruptData,
  KbOut,
  MessageOut,
  NodeUpdateData,
  SessionOut,
  TokenEventData,
  TraceEventData,
} from "@/lib/types";

interface ChatMsg {
  id: string;
  role: "user" | "assistant";
  content: string;
  timeline: TimelineItem[];
  citations: CitationData[];
  done?: DoneData;
  errorText?: string;
  interrupt?: InterruptData;
  streaming?: boolean;
}

const SAMPLE_QUESTIONS = [
  "用泰勒展开解释 $e^x$ 的近似计算",
  "什么是贝叶斯定理？举一个生活中的例子",
  "帮我梳理二次函数 $y=ax^2+bx+c$ 的图像性质",
];

function genId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export default function ChatPage() {
  // 会话与知识库
  const [sessions, setSessions] = useState<SessionOut[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [kbs, setKbs] = useState<KbOut[]>([]);
  const [kbId, setKbId] = useState("");

  // 消息与流式状态
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // 引用联动
  const [activeCitation, setActiveCitation] = useState<number | null>(null);
  const [citationMsgId, setCitationMsgId] = useState<string | null>(null);

  // 移动端抽屉
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [citationsOpen, setCitationsOpen] = useState(false);

  const [banner, setBanner] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const patchMsg = useCallback(
    (id: string, patch: (m: ChatMsg) => ChatMsg) => {
      setMessages((prev) => prev.map((m) => (m.id === id ? patch(m) : m)));
    },
    [],
  );

  const loadSessions = useCallback(async () => {
    try {
      const list = await apiJson<SessionOut[]>("/api/chat/sessions");
      setSessions(list);
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "会话列表加载失败");
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  // 首屏并行拉取会话列表与知识库列表
  useEffect(() => {
    let alive = true;
    (async () => {
      const [sessionsRes, kbsRes] = await Promise.allSettled([
        apiJson<SessionOut[]>("/api/chat/sessions"),
        apiJson<KbOut[]>("/api/kb"),
      ]);
      if (!alive) return;
      if (sessionsRes.status === "fulfilled") setSessions(sessionsRes.value);
      else setBanner("会话列表加载失败");
      setSessionsLoading(false);
      if (kbsRes.status === "fulfilled") setKbs(kbsRes.value);
      else setBanner("知识库列表加载失败");
    })();
    return () => {
      alive = false;
    };
  }, []);

  // 新消息 / 新 token 时滚到底部
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const selectSession = useCallback(async (id: string) => {
    abortRef.current?.abort();
    setActiveSessionId(id);
    setSessionsOpen(false);
    setActiveCitation(null);
    setCitationMsgId(null);
    try {
      const history = await apiJson<MessageOut[]>(
        `/api/chat/sessions/${id}/messages`,
      );
      setMessages(
        history.map((m) => ({
          id: m.id,
          role: m.role === "user" ? "user" : "assistant",
          content: m.content,
          timeline: [],
          citations: [],
        })),
      );
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "历史消息加载失败");
    }
  }, []);

  const newSession = useCallback(() => {
    abortRef.current?.abort();
    setActiveSessionId(null);
    setMessages([]);
    setActiveCitation(null);
    setCitationMsgId(null);
    setSessionsOpen(false);
  }, []);

  const renameSession = useCallback(
    async (id: string, title: string) => {
      try {
        await apiJson<SessionOut>(`/api/chat/sessions/${id}`, {
          method: "PATCH",
          body: { title },
        });
        await loadSessions();
      } catch (err) {
        setBanner(err instanceof ApiError ? err.message : "重命名失败");
      }
    },
    [loadSessions],
  );

  const deleteSession = useCallback(
    async (id: string) => {
      try {
        await apiJson<void>(`/api/chat/sessions/${id}`, { method: "DELETE" });
        if (id === activeSessionId) {
          setActiveSessionId(null);
          setMessages([]);
        }
        await loadSessions();
      } catch (err) {
        setBanner(err instanceof ApiError ? err.message : "删除失败");
      }
    },
    [activeSessionId, loadSessions],
  );

  const send = useCallback(
    async (raw?: string) => {
      const text = (raw ?? input).trim();
      if (!text || streaming) return;
      setInput("");
      setBanner(null);

      const userMsg: ChatMsg = {
        id: genId(),
        role: "user",
        content: text,
        timeline: [],
        citations: [],
      };
      const asstId = genId();
      const asstMsg: ChatMsg = {
        id: asstId,
        role: "assistant",
        content: "",
        timeline: [],
        citations: [],
        streaming: true,
      };
      setMessages((prev) => [...prev, userMsg, asstMsg]);
      setStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const body = {
          message: text,
          session_id: activeSessionId ?? undefined,
          kb_id: kbId || undefined,
        };
        for await (const msg of streamSse("/api/chat", body, {
          signal: controller.signal,
        })) {
          switch (msg.event) {
            case "token": {
              const d = msg.data as TokenEventData;
              patchMsg(asstId, (m) => ({ ...m, content: m.content + d.text }));
              break;
            }
            case "node_update": {
              const d = msg.data as NodeUpdateData;
              patchMsg(asstId, (m) => ({
                ...m,
                timeline: [...m.timeline, { kind: "node", data: d }],
              }));
              break;
            }
            case "trace_event": {
              const d = msg.data as TraceEventData;
              patchMsg(asstId, (m) => ({
                ...m,
                timeline: [...m.timeline, { kind: "trace", data: d }],
              }));
              break;
            }
            case "citation": {
              const d = msg.data as CitationData;
              patchMsg(asstId, (m) => ({
                ...m,
                citations: [...m.citations, d],
              }));
              setCitationMsgId(asstId);
              break;
            }
            case "interrupt": {
              const d = msg.data as InterruptData;
              patchMsg(asstId, (m) => ({ ...m, interrupt: d }));
              break;
            }
            case "done": {
              const d = msg.data as DoneData;
              patchMsg(asstId, (m) => ({ ...m, done: d }));
              if (d.session_id) setActiveSessionId(d.session_id);
              void loadSessions();
              break;
            }
            case "error": {
              const d = msg.data as ErrorEventData;
              patchMsg(asstId, (m) => ({
                ...m,
                errorText: d.message || d.code,
              }));
              break;
            }
            default:
              break; // 未知事件向前兼容：忽略
          }
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") {
          patchMsg(asstId, (m) => ({
            ...m,
            content: m.content ? `${m.content}\n（已停止生成）` : "（已停止生成）",
          }));
        } else {
          const msg =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : "网络异常，请稍后重试";
          patchMsg(asstId, (m) => ({ ...m, errorText: msg }));
        }
      } finally {
        patchMsg(asstId, (m) => ({ ...m, streaming: false }));
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [input, streaming, activeSessionId, kbId, patchMsg, loadSessions],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const handleComposerKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  const handleCitationClick = useCallback((msgId: string, idx: number) => {
    setCitationMsgId(msgId);
    setActiveCitation((prev) => (prev === idx ? null : idx));
    setCitationsOpen(true);
  }, []);

  // 引用面板数据源：优先用户选中的消息，否则最近一条带引用的助手消息
  const citedMsg =
    messages.find((m) => m.id === citationMsgId && m.citations.length > 0) ??
    [...messages].reverse().find(
      (m) => m.role === "assistant" && m.citations.length > 0,
    );
  const panelCitations = citedMsg?.citations ?? [];

  const citationsPanel = (
    <CitationsPanel
      citations={panelCitations}
      activeIdx={activeCitation}
      onSelect={(idx) =>
        setActiveCitation((prev) => (prev === idx ? null : idx))
      }
    />
  );

  const sessionList = (
    <SessionList
      sessions={sessions}
      activeId={activeSessionId}
      loading={sessionsLoading}
      onSelect={(id) => void selectSession(id)}
      onNew={newSession}
      onRename={renameSession}
      onDelete={deleteSession}
    />
  );

  return (
    <div className="flex h-full min-h-0">
      {/* 左：会话列表（桌面） */}
      <aside className="hidden w-64 shrink-0 border-r border-slate-200 bg-white md:block">
        {sessionList}
      </aside>

      {/* 左：会话列表（移动端抽屉） */}
      {sessionsOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <button
            type="button"
            aria-label="关闭会话列表"
            className="absolute inset-0 bg-slate-900/40"
            onClick={() => setSessionsOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 w-72 bg-white shadow-xl">
            {sessionList}
          </div>
        </div>
      )}

      {/* 中：消息区 */}
      <section className="flex min-w-0 flex-1 flex-col">
        {/* 工具栏：知识库选择器 */}
        <div className="flex shrink-0 items-center gap-2 border-b border-slate-200 bg-white px-3 py-2">
          <button
            type="button"
            onClick={() => setSessionsOpen(true)}
            className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 md:hidden"
            aria-label="打开会话列表"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-5 w-5"
              aria-hidden
            >
              <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
            </svg>
          </button>

          <label className="flex min-w-0 items-center gap-2">
            <span className="hidden shrink-0 text-xs font-medium text-slate-500 sm:block">
              知识库
            </span>
            <select
              value={kbId}
              onChange={(e) => setKbId(e.target.value)}
              className="max-w-56 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-700 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
            >
              <option value="">不绑定（通用模式）</option>
              {kbs.map((kb) => (
                <option key={kb.id} value={kb.id}>
                  {kb.name}
                </option>
              ))}
            </select>
          </label>
          {kbId && (
            <span className="hidden rounded-full bg-indigo-50 px-2 py-0.5 text-xs text-indigo-600 sm:block">
              引用溯源问答
            </span>
          )}

          <span className="flex-1" />

          <button
            type="button"
            onClick={() => setCitationsOpen(true)}
            className="relative rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 xl:hidden"
            aria-label="打开引用面板"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-5 w-5"
              aria-hidden
            >
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6" />
            </svg>
            {panelCitations.length > 0 && (
              <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-indigo-600 px-1 text-[10px] font-semibold text-white">
                {panelCitations.length}
              </span>
            )}
          </button>
        </div>

        {banner && (
          <div className="flex items-center justify-between gap-2 border-b border-red-100 bg-red-50 px-4 py-2 text-sm text-red-700">
            <span className="min-w-0 truncate">{banner}</span>
            <button
              type="button"
              onClick={() => setBanner(null)}
              className="shrink-0 text-red-400 hover:text-red-600"
              aria-label="关闭提示"
            >
              ✕
            </button>
          </div>
        )}

        {/* 消息流 */}
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-6">
            {messages.length === 0 ? (
              <div className="flex flex-col items-center gap-4 py-16 text-center">
                <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-indigo-50 text-indigo-500">
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-7 w-7"
                    aria-hidden
                  >
                    <path d="M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z" />
                  </svg>
                </span>
                <div>
                  <h2 className="text-lg font-semibold text-slate-900">
                    开始新的对话
                  </h2>
                  <p className="mt-1 text-sm text-slate-500">
                    支持数学公式渲染；绑定知识库后回答自带引用溯源。
                  </p>
                </div>
                <div className="flex flex-wrap justify-center gap-2">
                  {SAMPLE_QUESTIONS.map((q) => (
                    <button
                      key={q}
                      type="button"
                      onClick={() => void send(q)}
                      className="rounded-full border border-slate-200 bg-white px-3.5 py-1.5 text-sm text-slate-600 transition-colors hover:border-indigo-300 hover:text-indigo-700"
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((m) =>
                m.role === "user" ? (
                  <div key={m.id} className="flex justify-end">
                    <div className="max-w-[85%] rounded-2xl rounded-br-md bg-indigo-600 px-4 py-2.5 text-sm leading-relaxed text-white">
                      <span className="whitespace-pre-wrap break-words">
                        {m.content}
                      </span>
                    </div>
                  </div>
                ) : (
                  <div key={m.id} className="flex justify-start">
                    <div className="w-full max-w-[95%]">
                      <ProcessTimeline
                        items={m.timeline}
                        streaming={Boolean(m.streaming)}
                      />

                      {m.done?.refused && (
                        <div className="mb-2 flex items-center gap-1.5 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-700">
                          <svg
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="1.8"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            className="h-3.5 w-3.5 shrink-0"
                            aria-hidden
                          >
                            <path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
                          </svg>
                          已拒答：知识库中未检索到足够依据，为避免幻觉未调用模型作答。
                        </div>
                      )}

                      {(m.content || m.streaming) && (
                        <div
                          className={`rounded-2xl rounded-bl-md border px-4 py-2.5 text-sm leading-relaxed text-slate-800 ${
                            m.done?.refused
                              ? "border-amber-200 bg-amber-50/50"
                              : "border-slate-200 bg-white"
                          }`}
                        >
                          {m.content ? (
                            <MathText
                              text={m.content}
                              streaming={Boolean(m.streaming)}
                              onCitationClick={(idx) =>
                                handleCitationClick(m.id, idx)
                              }
                              activeCitation={
                                citedMsg?.id === m.id ? activeCitation : null
                              }
                            />
                          ) : (
                            <span className="inline-flex items-center gap-1.5 text-slate-400">
                              <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-indigo-200 border-t-indigo-600" />
                              正在思考…
                            </span>
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

                      {m.done?.usage && (
                        <p className="mt-1.5 px-1 text-xs text-slate-400">
                          {m.done.usage.latency_ms != null &&
                            `耗时 ${m.done.usage.latency_ms}ms`}
                          {m.done.usage.cost_cny != null &&
                            ` · ¥${m.done.usage.cost_cny}`}
                          {m.done.usage.mode && ` · ${m.done.usage.mode}`}
                          {m.citations.length > 0 &&
                            ` · ${m.citations.length} 条引用`}
                        </p>
                      )}
                    </div>
                  </div>
                ),
              )
            )}
          </div>
        </div>

        {/* 输入区 */}
        <div className="shrink-0 border-t border-slate-200 bg-white px-4 py-3">
          <div className="mx-auto flex w-full max-w-3xl items-end gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleComposerKey}
              rows={2}
              maxLength={8000}
              placeholder={
                streaming ? "正在生成回答…" : "输入问题，Enter 发送，Shift+Enter 换行"
              }
              disabled={streaming}
              className="min-h-11 flex-1 resize-none rounded-xl border border-slate-300 px-3.5 py-2.5 text-sm text-slate-900 outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-50"
            />
            {streaming ? (
              <button
                type="button"
                onClick={stop}
                className="flex h-11 shrink-0 items-center gap-1.5 rounded-xl border border-slate-300 px-4 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-100"
              >
                <span className="h-2.5 w-2.5 rounded-sm bg-slate-500" />
                停止
              </button>
            ) : (
              <button
                type="button"
                onClick={() => void send()}
                disabled={!input.trim()}
                className="flex h-11 shrink-0 items-center gap-1.5 rounded-xl bg-indigo-600 px-4 text-sm font-semibold text-white transition-colors hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                发送
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-4 w-4"
                  aria-hidden
                >
                  <path d="m22 2-7 20-4-9-9-4Zm0 0L11 13" />
                </svg>
              </button>
            )}
          </div>
        </div>
      </section>

      {/* 右：引用面板（桌面） */}
      <aside className="hidden w-80 shrink-0 flex-col border-l border-slate-200 bg-white xl:flex">
        <div className="flex h-11 shrink-0 items-center border-b border-slate-100 px-4">
          <h2 className="text-sm font-semibold text-slate-800">
            引用溯源
            {panelCitations.length > 0 && (
              <span className="ml-1.5 text-xs font-normal text-slate-400">
                {panelCitations.length} 条
              </span>
            )}
          </h2>
        </div>
        <div className="min-h-0 flex-1">{citationsPanel}</div>
      </aside>

      {/* 右：引用面板（移动端抽屉） */}
      {citationsOpen && (
        <div className="fixed inset-0 z-40 xl:hidden">
          <button
            type="button"
            aria-label="关闭引用面板"
            className="absolute inset-0 bg-slate-900/40"
            onClick={() => setCitationsOpen(false)}
          />
          <div className="absolute inset-y-0 right-0 flex w-80 max-w-[85vw] flex-col bg-white shadow-xl">
            <div className="flex h-11 shrink-0 items-center justify-between border-b border-slate-100 px-4">
              <h2 className="text-sm font-semibold text-slate-800">引用溯源</h2>
              <button
                type="button"
                onClick={() => setCitationsOpen(false)}
                className="text-slate-400 hover:text-slate-600"
                aria-label="关闭"
              >
                ✕
              </button>
            </div>
            <div className="min-h-0 flex-1">{citationsPanel}</div>
          </div>
        </div>
      )}
    </div>
  );
}
