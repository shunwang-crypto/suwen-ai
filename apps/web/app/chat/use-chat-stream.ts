"use client";

// 对话页流式发送域（A 瘦身自 page.tsx 拆出）：
// 消息列表 / SSE 订阅（token 增量、节点/轨迹时间线、引用、followups、interrupt、done）/
// 停止与重发 / 视图纪元（切会话防旧流覆盖当前视图）。
// 页面侧的会话切换通过 setMessages/activeRunId/viewEpochRef 与本域协作。

import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

import { ChatMode, ChatMsg } from "@/components/chat/chat-msg";
import { type TimelineItem } from "@/components/chat/timeline";
import { ApiError, apiJson } from "@/lib/api";
import { streamSse } from "@/lib/sse";
import type {
  ChatAttachmentOut,
  CitationData,
  DoneData,
  ErrorEventData,
  FollowupsEventData,
  InterruptData,
  KbOut,
  NodeUpdateData,
  SessionEventData,
  TokenEventData,
  TraceEventData,
} from "@/lib/types";
import { genId, normalizeAnswerCitations, rememberChatSession } from "./view-helpers";
import type { SessionIndicator } from "./view-helpers";

export interface UseChatStreamOptions {
  input: string;
  setInput: (value: string) => void;
  attachments: ChatAttachmentOut[];
  setAttachments: Dispatch<SetStateAction<ChatAttachmentOut[]>>;
  chatMode: ChatMode;
  kbIds: string[];
  kbs: KbOut[];
  activeSessionId: string | null;
  setActiveSessionId: Dispatch<SetStateAction<string | null>>;
  setSessionIndicators: Dispatch<SetStateAction<Record<string, SessionIndicator>>>;
  loadSessions: () => void | Promise<void>;
  setBanner: Dispatch<SetStateAction<string | null>>;
  setComposerPanel: Dispatch<SetStateAction<"menu" | "mode" | "scope" | null>>;
  /** 引用联动：流内 citation 事件点亮消息角标，done 时清当前选中 */
  setActiveCitation: Dispatch<SetStateAction<number | null>>;
  setCitationMsgId: Dispatch<SetStateAction<string | null>>;
}

export function useChatStream(options: UseChatStreamOptions) {
  const {
    input,
    setInput,
    attachments,
    setAttachments,
    chatMode,
    kbIds,
    kbs,
    activeSessionId,
    setActiveSessionId,
    setSessionIndicators,
    loadSessions,
    setBanner,
    setComposerPanel,
    setActiveCitation,
    setCitationMsgId,
  } = options;

  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const detachedControllersRef = useRef(new WeakSet<AbortController>());
  const stoppedControllersRef = useRef(new WeakSet<AbortController>());
  /** 每次切换会话/新建会话递增，防旧流完成后覆盖当前视图。 */
  const viewEpochRef = useRef(0);

  const patchMsg = useCallback(
    (id: string, patch: (m: ChatMsg) => ChatMsg) => {
      setMessages((prev) => prev.map((m) => (m.id === id ? patch(m) : m)));
    },
    [],
  );

  const detachCurrentStream = useCallback(() => {
    const controller = abortRef.current;
    if (!controller) return;
    detachedControllersRef.current.add(controller);
    controller.abort();
    if (abortRef.current === controller) abortRef.current = null;
  }, []);

  // 离开 /chat 只断开当前页面的 SSE 订阅；后端生成任务继续运行并最终落库。
  useEffect(
    () => () => {
      viewEpochRef.current += 1;
      detachCurrentStream();
    },
    [detachCurrentStream],
  );

  const send = useCallback(
    async (raw?: string) => {
      const sentAttachments = attachments;
      const typed = (raw ?? input).trim();
      const text = typed || (sentAttachments.length > 0 ? "请分析这些附件" : "");
      if (!text || streaming) return;
      if (
        (chatMode === "web" || chatMode === "research") &&
        sentAttachments.length > 0
      ) {
        setBanner("联网模式暂不能与附件同时使用，请移除附件或切换回答方式");
        return;
      }
      setInput("");
      setAttachments([]);
      setComposerPanel(null);
      setBanner(null);

      const userMsg: ChatMsg = {
        id: genId(),
        role: "user",
        content: text,
        timeline: [],
        citations: [],
        attachments: sentAttachments,
        chatMode,
      };
      const asstId = genId();
      const initialTimeline: TimelineItem[] =
        kbIds.length > 0 && (chatMode === "fast" || chatMode === "think")
          ? [
              {
                kind: "node",
                data: {
                  node: chatMode === "fast" ? "cache_lookup" : "retrieve",
                  status: chatMode === "fast" ? "running" : "pending",
                  detail:
                    chatMode === "fast"
                      ? "正在匹配可复用回答"
                      : "正在准备检索资料",
                  synthetic: true,
                },
              },
            ]
          : [];
      const asstMsg: ChatMsg = {
        id: asstId,
        role: "assistant",
        content: "",
        timeline: initialTimeline,
        citations: [],
        streaming: true,
        attempt: 0,
        kbId: kbIds[0] || undefined,
        kbName: kbs.find((kb) => kb.id === kbIds[0])?.name,
        chatMode,
      };
      setMessages((prev) => [...prev, userMsg, asstMsg]);
      setStreaming(true);
      setActiveRunId(null);
      const runEpoch = viewEpochRef.current;

      const controller = new AbortController();
      abortRef.current = controller;
      let accepted = false;

      try {
        const body = {
          message: text,
          session_id: activeSessionId ?? undefined,
          mode: chatMode,
          kb_ids: kbIds.length > 0 ? kbIds : undefined,
          attachment_ids:
            sentAttachments.length > 0
              ? sentAttachments.map((item) => item.id)
              : undefined,
        };
        for await (const msg of streamSse("/api/chat", body, {
          signal: controller.signal,
        })) {
          if (viewEpochRef.current !== runEpoch) continue;
          switch (msg.event) {
            case "session": {
              const d = msg.data as SessionEventData;
              accepted = true;
              setActiveSessionId(d.session_id);
              setActiveRunId(d.run_id);
              setSessionIndicators((prev) => ({
                ...prev,
                [d.session_id]: "running",
              }));
              rememberChatSession(d.session_id);
              // 新会话在模型完成前就进入左栏，切走后仍可随时点回来查看运行状态。
              void loadSessions();
              break;
            }
            case "token": {
              const d = msg.data as TokenEventData;
              const attempt = d.attempt ?? 0;
              patchMsg(asstId, (m) =>
                attempt !== (m.attempt ?? 0)
                  ? // verify 触发重生成：attempt 变化即清空重排，防拼接双答案
                    { ...m, content: d.text, attempt, stalled: false }
                  : { ...m, content: m.content + d.text, stalled: false },
              );
              break;
            }
            case "stall": {
              // 客户端合成事件（lib/sse.ts）：45s 无任何服务端事件，软超时提示
              patchMsg(asstId, (m) => ({ ...m, stalled: true }));
              break;
            }
            case "node_update": {
              const d = msg.data as NodeUpdateData;
              patchMsg(asstId, (m) => ({
                ...m,
                timeline: [
                  ...m.timeline.filter(
                    (item) => item.kind !== "node" || item.data.synthetic !== true,
                  ),
                  { kind: "node", data: d },
                ],
              }));
              break;
            }
            case "trace_event": {
              const d = msg.data as TraceEventData;
              patchMsg(asstId, (m) => ({
                ...m,
                timeline: [
                  ...m.timeline.filter(
                    (item) => item.kind !== "node" || item.data.synthetic !== true,
                  ),
                  { kind: "trace", data: d },
                ],
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
            case "followups": {
              const d = msg.data as FollowupsEventData;
              patchMsg(asstId, (m) => ({
                ...m,
                followups: Array.isArray(d.items) ? d.items : [],
              }));
              break;
            }
            case "interrupt": {
              const d = msg.data as InterruptData;
              patchMsg(asstId, (m) => ({ ...m, interrupt: d }));
              break;
            }
            case "done": {
              const d = msg.data as DoneData;
              patchMsg(asstId, (m) => {
                const normalized = normalizeAnswerCitations(m.content, m.citations);
                return {
                  ...m,
                  content: normalized.content,
                  citations: normalized.citations,
                  done: d,
                  stalled: false,
                };
              });
              setActiveCitation(null);
              if (d.session_id) {
                setActiveSessionId(d.session_id);
                // 当前会话的答案已经呈现在眼前，完成时直接记为已读。
                setSessionIndicators((prev) => {
                  if (!(d.session_id in prev)) return prev;
                  const next = { ...prev };
                  delete next[d.session_id];
                  return next;
                });
              }
              setActiveRunId(null);
              void loadSessions();
              break;
            }
            case "error": {
              const d = msg.data as ErrorEventData;
              patchMsg(asstId, (m) => ({
                ...m,
                errorText: d.message || d.code,
              }));
              if (activeSessionId) {
                setSessionIndicators((prev) => {
                  if (!(activeSessionId in prev)) return prev;
                  const next = { ...prev };
                  delete next[activeSessionId];
                  return next;
                });
              }
              break;
            }
            default:
              break; // 未知事件向前兼容：忽略
          }
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") {
          // 切换会话/页面只是解绑订阅，后台继续；只有显式停止才标记半截回答。
          if (
            stoppedControllersRef.current.has(controller) &&
            viewEpochRef.current === runEpoch
          ) {
            patchMsg(asstId, (m) => ({
              ...m,
              truncated: true,
              stalled: false,
            }));
          }
        } else {
          const msg =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : "网络异常，请稍后重试";
          patchMsg(asstId, (m) => ({ ...m, errorText: msg }));
        }
        if (!accepted && sentAttachments.length > 0) {
          setAttachments((prev) => [...sentAttachments, ...prev]);
        }
      } finally {
        if (viewEpochRef.current === runEpoch) {
          patchMsg(asstId, (m) => ({
            ...m,
            timeline: m.timeline.filter(
              (item) => item.kind !== "node" || item.data.synthetic !== true,
            ),
            streaming: false,
          }));
          setStreaming(false);
          setActiveRunId(null);
        }
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [
      input,
      attachments,
      streaming,
      activeSessionId,
      chatMode,
      kbIds,
      kbs,
      patchMsg,
      loadSessions,
      setSessionIndicators,
      setActiveSessionId,
      setActiveCitation,
      setCitationMsgId,
      setAttachments,
      setBanner,
      setComposerPanel,
      setInput,
    ],
  );

  const stop = useCallback(() => {
    if (!activeRunId) {
      setBanner("任务正在初始化，请稍后再停止");
      return;
    }
    void apiJson(`/api/chat/runs/${activeRunId}/stop`, { method: "POST" }).catch(
      (err) => {
        if (!(err instanceof ApiError && err.status === 404)) {
          setBanner(err instanceof ApiError ? err.message : "停止任务失败");
        }
      },
    );
    const controller = abortRef.current;
    if (controller) {
      stoppedControllersRef.current.add(controller);
      controller.abort();
      if (abortRef.current === controller) abortRef.current = null;
    }
    setActiveRunId(null);
    setStreaming(false);
    if (activeSessionId) {
      setSessionIndicators((prev) => {
        if (!(activeSessionId in prev)) return prev;
        const next = { ...prev };
        delete next[activeSessionId];
        return next;
      });
    }
  }, [activeRunId, activeSessionId, setBanner, setSessionIndicators]);

  /** truncated 消息的「重新提问」：回溯到该助手消息之前最近的用户消息重发 */
  const retryFrom = useCallback(
    (assistantId: string) => {
      const idx = messages.findIndex((m) => m.id === assistantId);
      for (let i = (idx === -1 ? messages.length : idx) - 1; i >= 0; i--) {
        if (messages[i].role === "user") {
          void send(messages[i].content);
          return;
        }
      }
    },
    [messages, send],
  );

  return {
    messages,
    setMessages,
    streaming,
    setStreaming,
    activeRunId,
    setActiveRunId,
    viewEpochRef,
    patchMsg,
    detachCurrentStream,
    send,
    stop,
    retryFrom,
  };
}
