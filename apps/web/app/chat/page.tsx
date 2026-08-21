"use client";

// 主对话界面：会话列表 / 流式消息（token 增量）/ 引用面板。
// SSE 走 fetch+ReadableStream（lib/sse.ts），支持契约七类事件。

import { useRouter, useSearchParams } from "next/navigation";
import {Suspense, useCallback, useEffect, useMemo, useRef, useState, KeyboardEvent} from "react";

import { ChatToolbar } from "@/components/chat/chat-toolbar";
import { TutorWorkspace } from "@/components/tutor/tutor-workspace";
import { CitationsPanel } from "@/components/chat/citations-panel";
import { ContextDrawer } from "@/components/chat/context-drawer";
import { SessionList } from "@/components/chat/session-list";
import { ApiError, apiJson } from "@/lib/api";
import { ChatMode, ChatMsg } from "@/components/chat/chat-msg";
import { MessageList } from "@/components/chat/message-list";
import { Composer } from "@/components/chat/composer";
import {
  booleanStorageCodec,
  useHydratedLocalStorage,
} from "@/lib/use-hydrated-local-storage";
import type {
  ChatRunStatusOut,
  ChatAttachmentCapabilities,
  CitationData,
  KbOut,
  MessageOut,
  SessionOut,
} from "@/lib/types";
import { useApiResource } from "@/lib/use-api-resource";
import { useChatAttachments } from "./use-chat-attachments";
import { useChatStream } from "./use-chat-stream";
import {
  CHAT_HISTORY_COLLAPSED_KEY,
  CHAT_SESSION_STATUS_KEY,
  EMPTY_SESSION_INDICATORS,
  LAST_CHAT_SESSION_KEY,
  historyToMessages,
  rememberChatSession,
  sessionIndicatorsCodec,
  wait,
} from "./view-helpers";

/**
 * 对话页主体。使用 useSearchParams（读 ?kb=&q=），生产构建要求包在 Suspense 里，
 * 因此默认导出是下面的 ChatPage 壳（Next 16 文档 use-search-params#prerendering）。
 */
function ChatView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // 会话与知识库
  const [sessions, setSessions] = useState<SessionOut[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  // Wave3 联合检索：多选（checkbox 列表，个人库与机构库分组），发送 kb_ids。
  // 初值取 ?kb=（学习指南 chip 带过来的目标库），保证首帧的 send 闭包就绑定了它。
  const [kbIds, setKbIds] = useState<string[]>(() => {
    const kb = searchParams.get("kb");
    return kb ? [kb] : [];
  });
  const [chatMode, setChatMode] = useState<ChatMode>("fast");
  const [composerPanel, setComposerPanel] = useState<
    "menu" | "mode" | "scope" | null
  >(null);
  const chatAttachmentInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [composerMultiline, setComposerMultiline] = useState(false);

  // 消息与流式状态（messages/streaming/activeRunId 等归 useChatStream, 见下方 hook 调用）
  /** 正在选"讲得不好"原因的消息 id (0031 M2) */
  const [ratingPick, setRatingPick] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sessionIndicators, setSessionIndicators] = useHydratedLocalStorage(
    CHAT_SESSION_STATUS_KEY,
    EMPTY_SESSION_INDICATORS,
    sessionIndicatorsCodec,
  );
  const restoredSessionRef = useRef(false);

  // 引用联动
  const [activeCitation, setActiveCitation] = useState<number | null>(null);
  const [citationMsgId, setCitationMsgId] = useState<string | null>(null);
  // 引用回原文抽屉（点击引用卡打开；kbId 取该消息发送时绑定的知识库）
  const [contextTarget, setContextTarget] = useState<{
    kbId: string;
    citation: CitationData;
  } | null>(null);

  // 移动端抽屉
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [historyCollapsed, setHistoryCollapsed] = useHydratedLocalStorage(
    CHAT_HISTORY_COLLAPSED_KEY,
    false,
    booleanStorageCodec,
  );
  const [citationsOpen, setCitationsOpen] = useState(false);

  const [banner, setBanner] = useState<string | null>(null);
  /** 辅导会话类型（阶段 6）：tutorNewOpen=新建辅导；activeTutorThreadId=打开的辅导线程 */
  const [tutorNewOpen, setTutorNewOpen] = useState(false);
  const [activeTutorThreadId, setActiveTutorThreadId] = useState<string | null>(null);
  const tutorMode = tutorNewOpen || activeTutorThreadId !== null;
  /** ?q= 自动发问只做一次（router.replace 清参数后 effect 会重跑） */
  const autoAskedRef = useRef(false);

  /** ?tutor=1 全局辅导入口只消费一次（旧 /tutor 路由重定向而来）。 */
  const tutorRequestedRef = useRef(false);
  useEffect(() => {
    if (searchParams.get("tutor") !== "1" || tutorRequestedRef.current) return;
    tutorRequestedRef.current = true;
    queueMicrotask(() => {
      setActiveTutorThreadId(null);
      setTutorNewOpen(true);
    });
  }, [searchParams]);
  /** ?new=1 全局新建入口只消费一次。 */
  const newChatRequestedRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (activeSessionId) {
      rememberChatSession(activeSessionId);
    }
  }, [activeSessionId]);

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

  // 首屏并行拉取会话、知识库与附件能力（A5 收敛: useApiResource + 派生/单向同步;
  // 会话列表的后续刷新仍走 loadSessions）
  const sessionsRes = useApiResource<SessionOut[]>("/api/chat/sessions", {
    errorMessage: "会话列表加载失败",
  });
  const kbListRes = useApiResource<KbOut[]>("/api/kb", {
    errorMessage: "知识库列表加载失败",
  });
  const attachmentRes = useApiResource<ChatAttachmentCapabilities>(
    "/api/chat/attachments/capabilities",
  );
  const kbs = useMemo(() => kbListRes.data ?? [], [kbListRes.data]);
  const attachmentCapabilities = attachmentRes.data ?? null;

  // 附件域（A 瘦身拆出）: 上传/粘贴/移除/发送前清空
  const {
    attachments,
    setAttachments,
    uploadingAttachment,
    discardPendingAttachments,
    removeAttachment,
    uploadChatAttachments,
    handleComposerPaste,
  } = useChatAttachments({
    attachmentCapabilities,
    setBanner,
    setComposerPanel,
    setChatMode,
  });

  /* eslint-disable react-hooks/set-state-in-effect -- 外部资源到达/失败 → 本地列表/横幅的单向同步, 无纯派生替代 */
  useEffect(() => {
    if (sessionsRes.data) setSessions(sessionsRes.data);
    else if (sessionsRes.error) setBanner("会话列表加载失败");
  }, [sessionsRes.data, sessionsRes.error]);

  useEffect(() => {
    setSessionsLoading(sessionsRes.loading);
  }, [sessionsRes.loading]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // 流式发送域（A 瘦身拆出）: 消息/SSE 订阅/停止/重发/视图纪元; 会话切换经
  // setMessages/activeRunId/viewEpochRef 与本域协作
  const {
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
  } = useChatStream({
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
  });

  // 新消息 / 新 token 时滚到底部
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // 默认保持一行；文字换行后才按内容自然增高，最多约六行后内部滚动。
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "24px";
    const nextHeight = Math.min(el.scrollHeight, 144);
    el.style.height = `${nextHeight}px`;
    el.style.overflowY = el.scrollHeight > 144 ? "auto" : "hidden";
    setComposerMultiline(nextHeight > 28);
  }, [input]);

  const selectSession = useCallback(async (id: string) => {
    discardPendingAttachments();
    detachCurrentStream();
    viewEpochRef.current += 1;
    setActiveSessionId(id);
    // 辅导会话（阶段 6）：中栏渲染 TutorWorkspace（GET /api/tutor/{id} 恢复现场），
    // 不走消息/run 拉取路径。
    const target = sessions.find((session) => session.id === id);
    if (target?.kind === "tutor") {
      setTutorNewOpen(false);
      setActiveTutorThreadId(id);
      setSessionIndicators((prev) => {
        if (prev[id] !== "completed") return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setActiveRunId(null);
      setStreaming(false);
      setMessages([]);
      setSessionsOpen(false);
      setActiveCitation(null);
      setCitationMsgId(null);
      rememberChatSession(id);
      return;
    }
    setActiveTutorThreadId(null);
    setTutorNewOpen(false);
    const epoch = viewEpochRef.current;
    // 蓝点代表“后台回答已完成但尚未查看”；进入会话即视为已读。
    setSessionIndicators((prev) => {
      if (prev[id] !== "completed") return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
    setActiveRunId(null);
    setStreaming(false);
    setSessionsOpen(false);
    setActiveCitation(null);
    setCitationMsgId(null);
    let restoredPlaceholderId: string | null = null;
    try {
      let [history, run] = await Promise.all([
        apiJson<MessageOut[]>(`/api/chat/sessions/${id}/messages`),
        apiJson<ChatRunStatusOut>(`/api/chat/sessions/${id}/run`),
      ]);
      if (viewEpochRef.current !== epoch) return;
      const restoredMessages = historyToMessages(history);
      // 回答方式是会话上下文的一部分；切换或刷新后从最后一条历史消息恢复，
      // 避免深度思考/联网模式在界面上错误回落为“极速”。
      setChatMode(restoredMessages.at(-1)?.chatMode ?? "fast");
      if (run.status !== "running" || !run.run_id) {
        setMessages(restoredMessages);
        setSessionIndicators((prev) => {
          if (!(id in prev)) return prev;
          const next = { ...prev };
          delete next[id];
          return next;
        });
        return;
      }

      // 运行中的回答尚未落库时，历史里只有用户消息。补一个临时助手消息，
      // 让刷新页面或切回会话后仍显示“正在思考”动画；完成后由真实历史替换。
      if (restoredMessages.at(-1)?.role !== "assistant") {
        restoredPlaceholderId = `running-${run.run_id}`;
        restoredMessages.push({
          id: restoredPlaceholderId,
          role: "assistant",
          content: "",
          timeline: [],
          citations: [],
          streaming: true,
          chatMode: restoredMessages.at(-1)?.chatMode ?? "fast",
        });
      }
      setMessages(restoredMessages);
      setSessionIndicators((prev) => ({ ...prev, [id]: "running" }));
      setActiveRunId(run.run_id);
      setStreaming(true);
      // SSE 已在切换时主动解绑；这里只轻量轮询任务状态，完成后一次性取最终历史。
      while (viewEpochRef.current === epoch && run.status === "running") {
        await wait(1200);
        if (viewEpochRef.current !== epoch) return;
        run = await apiJson<ChatRunStatusOut>(`/api/chat/sessions/${id}/run`);
      }
      if (viewEpochRef.current !== epoch) return;
      // 显式停止时，后台的 truncated 落库任务可能比状态清理晚几十毫秒。
      for (let attempt = 0; attempt < 5; attempt += 1) {
        history = await apiJson<MessageOut[]>(`/api/chat/sessions/${id}/messages`);
        if (history.at(-1)?.role === "assistant" || attempt === 4) break;
        await wait(120);
      }
      if (viewEpochRef.current !== epoch) return;
      setMessages(historyToMessages(history));
      setSessionIndicators((prev) => {
        if (!(id in prev)) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setActiveRunId(null);
      setStreaming(false);
      void loadSessions();
    } catch (err) {
      if (viewEpochRef.current !== epoch) return;
      const message = err instanceof ApiError ? err.message : "历史消息加载失败";
      if (restoredPlaceholderId) {
        patchMsg(restoredPlaceholderId, (item) => ({
          ...item,
          streaming: false,
          errorText: message,
        }));
      }
      setActiveRunId(null);
      setStreaming(false);
      setBanner(message);
    }
  }, [detachCurrentStream, discardPendingAttachments, loadSessions, patchMsg, sessions, setSessionIndicators, setActiveRunId, setMessages, setStreaming, viewEpochRef]);

  // 从其他功能页返回 /chat 时恢复上次查看的会话；带 ?q= 的推荐问题仍开启新对话。
  useEffect(() => {
    if (sessionsLoading || restoredSessionRef.current) return;
    restoredSessionRef.current = true;
    if (searchParams.get("q") || searchParams.get("new") === "1" || searchParams.get("session")) return;
    const remembered = window.localStorage.getItem(LAST_CHAT_SESSION_KEY);
    if (remembered && sessions.some((session) => session.id === remembered)) {
      const timer = window.setTimeout(() => void selectSession(remembered), 0);
      return () => window.clearTimeout(timer);
    } else if (remembered) {
      rememberChatSession(null);
    }
  }, [searchParams, selectSession, sessions, sessionsLoading]);

  // 侧栏「最近会话」带 ?session= 直达：选中后清参数，避免与上次会话恢复互相打架。
  useEffect(() => {
    const queued = searchParams.get("session");
    if (!queued || sessionsLoading) return;
    if (queued === activeSessionId) {
      router.replace("/chat");
      return;
    }
    if (!sessions.some((session) => session.id === queued)) {
      router.replace("/chat");
      return;
    }
    const timer = window.setTimeout(() => {
      void selectSession(queued);
      router.replace("/chat");
    }, 0);
    return () => window.clearTimeout(timer);
  }, [searchParams, sessions, sessionsLoading, activeSessionId, selectSession, router]);

  // 页面离开期间没有 SSE 订阅：只轮询本机记住的运行中会话，完成后换成蓝点。
  useEffect(() => {
    const knownIds = new Set(sessions.map((session) => session.id));
    const runningIds = Object.entries(sessionIndicators)
      .filter(([id, status]) => status === "running" && knownIds.has(id))
      .map(([id]) => id);
    if (runningIds.length === 0) return;

    let disposed = false;
    const refresh = async () => {
      const results = await Promise.allSettled(
        runningIds.map(async (id) => ({
          id,
          run: await apiJson<ChatRunStatusOut>(
            `/api/chat/sessions/${id}/run`,
          ),
        })),
      );
      if (disposed) return;
      setSessionIndicators((prev) => {
        let next = prev;
        for (const result of results) {
          if (
            result.status === "fulfilled" &&
            result.value.run.status === "idle" &&
            prev[result.value.id] === "running"
          ) {
            if (next === prev) next = { ...prev };
            if (result.value.id === activeSessionId) {
              delete next[result.value.id];
            } else {
              next[result.value.id] = "completed";
            }
          }
        }
        return next;
      });
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [activeSessionId, sessionIndicators, sessions, setSessionIndicators]);

  const newSession = useCallback(() => {
    discardPendingAttachments();
    detachCurrentStream();
    viewEpochRef.current += 1;
    setActiveSessionId(null);
    setTutorNewOpen(false);
    setActiveTutorThreadId(null);
    setActiveRunId(null);
    setStreaming(false);
    setMessages([]);
    setActiveCitation(null);
    setCitationMsgId(null);
    setSessionsOpen(false);
    rememberChatSession(null);
  }, [detachCurrentStream, discardPendingAttachments, setMessages, setActiveRunId, setStreaming, viewEpochRef]);

  // 全局导航的“新建对话”使用 ?new=1 明确请求空白会话，避免被上次会话恢复逻辑覆盖。
  useEffect(() => {
    if (searchParams.get("new") !== "1" || newChatRequestedRef.current) return;
    newChatRequestedRef.current = true;
    queueMicrotask(() => {
      newSession();
      router.replace("/chat");
    });
  }, [newSession, router, searchParams]);

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
      if (id === activeSessionId && activeRunId) {
        setBanner("回答仍在后台生成，请先点击停止后再删除会话");
        return false;
      }
      try {
        await apiJson<void>(`/api/chat/sessions/${id}`, { method: "DELETE" });
        if (id === activeSessionId) {
          setActiveSessionId(null);
          setMessages([]);
          rememberChatSession(null);
        }
        setSessionIndicators((prev) => {
          if (!(id in prev)) return prev;
          const next = { ...prev };
          delete next[id];
          return next;
        });
        await loadSessions();
        return true;
      } catch (err) {
        setBanner(err instanceof ApiError ? err.message : "删除失败");
        return false;
      }
    },
    [activeRunId, activeSessionId, loadSessions, setSessionIndicators, setMessages],
  );

  // 学习指南推荐问题 chip 跳转：/chat?kb=<id>&q=<question> —— 自动发问一次。
  // kb 参数在首帧就写进 kbIds 初值（见 useState 初始化），所以这里的 send 闭包
  // 已经带上目标知识库；发完把参数从地址栏清掉，刷新不会重复发问。
  useEffect(() => {
    if (autoAskedRef.current) return;
    const q = searchParams.get("q");
    if (!q) return;
    autoAskedRef.current = true;
    // 让位到微任务再发：send 会同步 setState，直接在 effect 体里调用等于在提交阶段
    // 触发级联渲染。不挂 cleanup —— 这是一次性动作，被 send 身份变化打断反而会丢发问。
    queueMicrotask(() => {
      void send(q);
      router.replace("/chat");
    });
  }, [searchParams, send, router]);

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

  const openMessageCitations = useCallback((msgId: string) => {
    setCitationMsgId(msgId);
    setActiveCitation(null);
    setCitationsOpen(true);
  }, []);

  // 回答评价 (0031 M2): 指纹取自该消息的 profile trace — 评的是"当时那版画像"
  const submitRating = useCallback(
    async (m: ChatMsg, rating: "good" | "bad", reason: string | null) => {
      const profileTrace = m.timeline.find(
        (item) => item.kind === "trace" && item.data?.type === "profile",
      );
      const weakPoints = Array.isArray(profileTrace?.data?.weak_points)
        ? (profileTrace?.data?.weak_points as unknown[])
            .filter((w): w is string => typeof w === "string")
            .slice(0, 5)
        : [];
      try {
        await apiJson("/api/profile/answer-ratings", {
          method: "POST",
          body: {
            message_id: m.id,
            rating,
            reason,
            weak_points: weakPoints,
            profile_used: profileTrace != null,
          },
        });
      } catch {
        // 评价是尽力而为: 失败不打断聊天, 也不弹错误 (下次可再评)
      }
      setMessages((prev) =>
        prev.map((row) =>
          row.id === m.id ? { ...row, rating, ratingReason: reason } : row,
        ),
      );
    },
    [setMessages],
  );

  // 引用面板数据源：优先用户选中的消息，否则最近一条带引用的助手消息
  const citedMsg =
    messages.find((m) => m.id === citationMsgId && m.citations.length > 0) ??
    [...messages].reverse().find(
      (m) => m.role === "assistant" && m.citations.length > 0,
    );
  const panelCitations = citedMsg?.citations ?? [];
  const citationsPanelTitle = panelCitations.every(
    (citation) => citation.source_type === "web",
  )
    ? "搜索结果"
    : panelCitations.every((citation) => citation.source_type !== "web")
      ? "知识库引用"
      : "引用依据";
  const activeSessionTitle =
    sessions.find((session) => session.id === activeSessionId)?.title ?? "新对话";
  const selectedKbLabel =
    kbIds.length === 0
      ? "选择范围"
      : kbIds.length === 1
        ? (kbs.find((kb) => kb.id === kbIds[0])?.name ?? "1 个知识库")
        : `已选 ${kbIds.length} 个库`;
  const selectedModeLabel =
    chatMode === "fast"
      ? "极速"
      : chatMode === "think"
        ? "深度思考"
        : chatMode === "web"
          ? "网页搜索"
          : "深度搜索";

  // 引用卡点击 → 打开原文抽屉。多库联合检索时优先用引用自带的来源库 kb_id，
  // 回退到该消息发送时绑定的首库
  const citedKbId = citedMsg?.kbId;
  const openContext = useCallback(
    (citation: CitationData) => {
      if (citation.source_type === "web") return;
      const kbForCitation = citation.kb_id || citedKbId;
      if (!kbForCitation) return;
      setContextTarget({ kbId: kbForCitation, citation });
    },
    [citedKbId],
  );

  // 库徽章配色区分个人/机构（纯展示映射，供引用卡片着色）
  const kbOwnerTypes = Object.fromEntries(
    kbs.map((k) => [k.id, k.owner_type]),
  );

  const citationsPanel = (
    <CitationsPanel
      citations={panelCitations}
      activeIdx={activeCitation}
      onSelect={(idx) =>
        setActiveCitation((prev) => (prev === idx ? null : idx))
      }
      onOpenContext={openContext}
      kbOwnerTypes={kbOwnerTypes}
    />
  );

  const renderSessionList = (onCollapse: () => void) => (
    <SessionList
      sessions={sessions}
      activeId={activeSessionId}
      loading={sessionsLoading}
      indicators={sessionIndicators}
      onSelect={(id) => void selectSession(id)}
      onNew={newSession}
      onNewTutor={() => {
        discardPendingAttachments();
        detachCurrentStream();
        viewEpochRef.current += 1;
        setActiveSessionId(null);
        setActiveRunId(null);
        setStreaming(false);
        setMessages([]);
        setSessionsOpen(false);
        setActiveCitation(null);
        setCitationMsgId(null);
        setActiveTutorThreadId(null);
        setTutorNewOpen(true);
      }}
      onCollapse={onCollapse}
      onRename={renameSession}
      onDelete={deleteSession}
    />
  );

  return (
    <div className="flex h-full min-h-0 bg-canvas">
      {/* 左：会话列表（桌面） */}
      {!historyCollapsed && (
        <aside className="hidden w-[218px] shrink-0 border-r border-line bg-surface md:block">
          {renderSessionList(() => setHistoryCollapsed(true))}
        </aside>
      )}

      {/* 左：会话列表（移动端抽屉） */}
      {sessionsOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <button
            type="button"
            aria-label="关闭会话列表"
            className="absolute inset-0 bg-ink/40"
            onClick={() => setSessionsOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 w-72 bg-white shadow-xl">
            {renderSessionList(() => setSessionsOpen(false))}
          </div>
        </div>
      )}

      {/* 中：消息区 */}
      <section className="flex min-w-0 flex-1 flex-col bg-canvas">
        <ChatToolbar
          historyCollapsed={historyCollapsed}
          onExpandHistory={() => setHistoryCollapsed(false)}
          onOpenSessions={() => setSessionsOpen(true)}
          title={tutorMode ? (activeTutorThreadId ? activeSessionTitle : "辅导解题") : activeSessionTitle}
          citationCount={tutorMode ? 0 : panelCitations.length}
          onOpenCitations={() => setCitationsOpen(true)}
        />

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

        {/* 辅导会话（阶段 6）：题目输入/多轮引导/提示阶梯整体进中栏 */}
        {tutorMode ? (
          <div className="min-h-0 flex-1 overflow-y-auto">
            <TutorWorkspace
              key={activeTutorThreadId ?? "tutor-new"}
              initialThreadId={activeTutorThreadId}
              onSessionsChanged={loadSessions}
            />
          </div>
        ) : (
          <>
        {/* 消息流 */}
        <MessageList
          messages={messages}
          streaming={streaming}
          scrollRef={scrollRef}
          ratingPick={ratingPick}
          setRatingPick={setRatingPick}
          submitRating={(m, rating, reason) => void submitRating(m, rating, reason)}
          send={(q) => void send(q)}
          retryFrom={(id) => void retryFrom(id)}
          handleCitationClick={handleCitationClick}
          openMessageCitations={openMessageCitations}
          citedMsgId={citedMsg?.id ?? null}
          activeCitation={activeCitation}
        />

        <Composer
          input={input}
          setInput={setInput}
          send={() => void send()}
          stop={stop}
          streaming={streaming}
          attachments={attachments}
          uploadingAttachment={uploadingAttachment}
          uploadChatAttachments={(event) => void uploadChatAttachments(event)}
          removeAttachment={(attachment) => void removeAttachment(attachment)}
          attachmentCapabilities={attachmentCapabilities}
          chatAttachmentInputRef={chatAttachmentInputRef}
          textareaRef={textareaRef}
          composerMultiline={composerMultiline}
          setComposerMultiline={setComposerMultiline}
          composerPanel={composerPanel}
          setComposerPanel={setComposerPanel}
          kbIds={kbIds}
          setKbIds={setKbIds}
          kbs={kbs}
          chatMode={chatMode}
          setChatMode={setChatMode}
          setBanner={setBanner}
          selectedKbLabel={selectedKbLabel}
          selectedModeLabel={selectedModeLabel}
          handleComposerKey={handleComposerKey}
          handleComposerPaste={handleComposerPaste}
        />
          </>
        )}
      </section>

      {/* 右：引用面板（桌面）—— 用户点击引用后才打开 */}
      {citationsOpen && panelCitations.length > 0 && (
      <aside className="hidden w-[318px] shrink-0 flex-col border-l border-line bg-white xl:flex">
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-line px-4">
          <h2 className="text-base font-medium text-ink">
            {citationsPanelTitle}
            {panelCitations.length > 0 && (
              <span className="ml-2 inline-flex min-w-5 items-center justify-center rounded-full bg-brand-50 px-1.5 py-0.5 text-xs font-normal text-brand-700">
                {panelCitations.length}
              </span>
            )}
          </h2>
          <button
            type="button"
            onClick={() => setCitationsOpen(false)}
            className="rounded-control p-1.5 text-ink-muted hover:bg-surface-muted hover:text-ink-secondary"
            aria-label="关闭引用面板"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" className="h-4 w-4" aria-hidden>
              <path d="m6 6 12 12M18 6 6 18" />
            </svg>
          </button>
        </div>
        <div className="min-h-0 flex-1">{citationsPanel}</div>
      </aside>
      )}

      {/* 右：引用面板（移动端抽屉） */}
      {citationsOpen && panelCitations.length > 0 && (
        <div className="fixed inset-0 z-40 xl:hidden">
          <button
            type="button"
            aria-label="关闭引用面板"
            className="absolute inset-0 bg-ink/40"
            onClick={() => setCitationsOpen(false)}
          />
          <div className="absolute inset-y-0 right-0 flex w-80 max-w-[85vw] flex-col bg-white shadow-xl">
            <div className="flex h-11 shrink-0 items-center justify-between border-b border-line px-4">
              <h2 className="text-sm font-semibold text-ink">{citationsPanelTitle}</h2>
              <button
                type="button"
                onClick={() => setCitationsOpen(false)}
                className="text-ink-muted hover:text-ink-secondary"
                aria-label="关闭"
              >
                ✕
              </button>
            </div>
            <div className="min-h-0 flex-1">{citationsPanel}</div>
          </div>
        </div>
      )}

      {/* 引用回原文抽屉（点击引用卡打开；面包屑+页码+相邻上下文+命中高亮） */}
      {contextTarget && (
        <ContextDrawer
          kbId={contextTarget.kbId}
          citation={contextTarget.citation}
          onClose={() => setContextTarget(null)}
        />
      )}
    </div>
  );
}

export default function ChatPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center text-sm text-ink-muted">
          对话加载中…
        </div>
      }
    >
      <ChatView />
    </Suspense>
  );
}
