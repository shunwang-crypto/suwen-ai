"use client";

// 主对话界面：会话列表 / 流式消息（token 增量）/ 引用面板。
// SSE 走 fetch+ReadableStream（lib/sse.ts），支持契约七类事件。

import { useRouter, useSearchParams } from "next/navigation";
import {
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type CSSProperties,
  type KeyboardEvent,
} from "react";

import { CitationsPanel } from "@/components/chat/citations-panel";
import { ContextDrawer } from "@/components/chat/context-drawer";
import { SessionList } from "@/components/chat/session-list";
import {
  ProcessTimeline,
  type TimelineItem,
} from "@/components/chat/timeline";
import { ApiError, apiFetch, apiJson } from "@/lib/api";
import { ChatMode, ChatMsg, MODE_LABELS } from "@/components/chat/chat-msg";
import { MessageList } from "@/components/chat/message-list";
import { Composer } from "@/components/chat/composer";
import { MathText } from "@/lib/katex";
import { streamSse } from "@/lib/sse";
import {
  booleanStorageCodec,
  useHydratedLocalStorage,
  type StorageCodec,
} from "@/lib/use-hydrated-local-storage";
import type {
  ChatRunStatusOut,
  ChatAttachmentCapabilities,
  ChatAttachmentOut,
  CitationData,
  DoneData,
  ErrorEventData,
  FollowupsEventData,
  InterruptData,
  KbOut,
  MessageOut,
  NodeUpdateData,
  SessionOut,
  SessionEventData,
  TokenEventData,
  TraceEventData,
} from "@/lib/types";


/** done.usage.mode 中文化（P2-18）：内部枚举不直接暴露给用户 */

const LAST_CHAT_SESSION_KEY = "eduagents:last-chat-session";
const CHAT_SESSION_STATUS_KEY = "eduagents:chat-session-status";
const CHAT_HISTORY_COLLAPSED_KEY = "suwen:chat-history-collapsed";

type SessionIndicator = "running" | "completed";

const sessionIndicatorsCodec: StorageCodec<Record<string, SessionIndicator>> = {
  parse: (raw) => {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(
        ([, value]) => value === "running" || value === "completed",
      ),
    ) as Record<string, SessionIndicator>;
  },
  serialize: (value) => JSON.stringify(value),
};

const EMPTY_SESSION_INDICATORS: Record<string, SessionIndicator> = {};

function genId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const INLINE_CITATION_RE = /\[(\d{1,3})\]/g;

/** 按正文首次引用顺序压缩编号，同时保留未引用的检索结果供核查。 */
function normalizeAnswerCitations(
  content: string,
  citations: CitationData[],
): { content: string; citations: CitationData[] } {
  const unique: CitationData[] = [];
  const seenSources = new Set<string>();
  for (const citation of citations) {
    const sourceKey =
      citation.source_type === "web"
        ? `web:${citation.url || citation.chunk_id}`
        : `kb:${citation.kb_id || ""}:${citation.chunk_id}`;
    if (seenSources.has(sourceKey)) continue;
    seenSources.add(sourceKey);
    unique.push(citation);
  }

  const byOriginalIdx = new Map<number, CitationData>();
  for (const citation of unique) {
    if (!byOriginalIdx.has(citation.idx)) byOriginalIdx.set(citation.idx, citation);
  }
  const citedOriginalIndexes: number[] = [];
  const citedSet = new Set<number>();
  for (const match of content.matchAll(INLINE_CITATION_RE)) {
    const originalIdx = Number(match[1]);
    if (byOriginalIdx.has(originalIdx) && !citedSet.has(originalIdx)) {
      citedSet.add(originalIdx);
      citedOriginalIndexes.push(originalIdx);
    }
  }

  const citedSources = citedOriginalIndexes
    .map((idx) => byOriginalIdx.get(idx))
    .filter((citation): citation is CitationData => Boolean(citation));
  const citedSourceSet = new Set(citedSources);
  const ordered = [
    ...citedSources,
    ...unique.filter((citation) => !citedSourceSet.has(citation)),
  ];
  const newIndexByOriginal = new Map<number, number>();
  citedOriginalIndexes.forEach((originalIdx, index) => {
    newIndexByOriginal.set(originalIdx, index + 1);
  });
  const normalizedContent = content.replace(
    INLINE_CITATION_RE,
    (full, rawIdx: string) => {
      const mapped = newIndexByOriginal.get(Number(rawIdx));
      return mapped ? `[${mapped}]` : full;
    },
  );

  return {
    content: normalizedContent,
    citations: ordered.map((citation, index) => ({
      ...citation,
      idx: index + 1,
      cited: citedSourceSet.has(citation),
    })),
  };
}

function historyToMessages(history: MessageOut[]): ChatMsg[] {
  return history.map((m) => {
    const rawCitations = Array.isArray(m.meta?.citations)
      ? (m.meta.citations as CitationData[])
      : [];
    const normalized = normalizeAnswerCitations(m.content, rawCitations);
    return {
      id: m.id,
      role: m.role === "user" ? "user" : "assistant",
      content: normalized.content,
      timeline: [],
      citations: normalized.citations,
      followups: Array.isArray(m.meta?.followups)
        ? (m.meta.followups as string[])
        : [],
      truncated: Boolean(m.meta?.truncated),
      attachments: Array.isArray(m.meta?.attachments)
        ? (m.meta.attachments as ChatAttachmentOut[])
        : [],
      chatMode:
        m.meta?.chat_mode === "think" ||
        m.meta?.chat_mode === "web" ||
        m.meta?.chat_mode === "research"
          ? m.meta.chat_mode
          : "fast",
    };
  });
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
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
  const [kbs, setKbs] = useState<KbOut[]>([]);
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
  const [attachments, setAttachments] = useState<ChatAttachmentOut[]>([]);
  const [uploadingAttachment, setUploadingAttachment] = useState(false);
  const [attachmentCapabilities, setAttachmentCapabilities] =
    useState<ChatAttachmentCapabilities | null>(null);
  const chatAttachmentInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [composerMultiline, setComposerMultiline] = useState(false);

  // 消息与流式状态
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  /** 正在选"讲得不好"原因的消息 id (0031 M2) */
  const [ratingPick, setRatingPick] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [sessionIndicators, setSessionIndicators] = useHydratedLocalStorage(
    CHAT_SESSION_STATUS_KEY,
    EMPTY_SESSION_INDICATORS,
    sessionIndicatorsCodec,
  );
  const abortRef = useRef<AbortController | null>(null);
  const detachedControllersRef = useRef(new WeakSet<AbortController>());
  const stoppedControllersRef = useRef(new WeakSet<AbortController>());
  /** 每次切换会话/新建会话递增，防旧流完成后覆盖当前视图。 */
  const viewEpochRef = useRef(0);
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
  /** ?q= 自动发问只做一次（router.replace 清参数后 effect 会重跑） */
  const autoAskedRef = useRef(false);
  /** ?new=1 全局新建入口只消费一次。 */
  const newChatRequestedRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const patchMsg = useCallback(
    (id: string, patch: (m: ChatMsg) => ChatMsg) => {
      setMessages((prev) => prev.map((m) => (m.id === id ? patch(m) : m)));
    },
    [],
  );

  const discardPendingAttachments = useCallback(() => {
    if (attachments.length === 0) return;
    const pending = attachments;
    setAttachments([]);
    void Promise.allSettled(
      pending.map((attachment) =>
        apiJson<void>(`/api/chat/attachments/${attachment.id}`, {
          method: "DELETE",
        }),
      ),
    );
  }, [attachments]);

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

  useEffect(() => {
    if (activeSessionId) {
      window.localStorage.setItem(LAST_CHAT_SESSION_KEY, activeSessionId);
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

  // 首屏并行拉取会话、知识库与附件能力
  useEffect(() => {
    let alive = true;
    (async () => {
      const [sessionsRes, kbsRes, attachmentRes] = await Promise.allSettled([
        apiJson<SessionOut[]>("/api/chat/sessions"),
        apiJson<KbOut[]>("/api/kb"),
        apiJson<ChatAttachmentCapabilities>(
          "/api/chat/attachments/capabilities",
        ),
      ]);
      if (!alive) return;
      if (sessionsRes.status === "fulfilled") setSessions(sessionsRes.value);
      else setBanner("会话列表加载失败");
      setSessionsLoading(false);
      if (kbsRes.status === "fulfilled") setKbs(kbsRes.value);
      else setBanner("知识库列表加载失败");
      if (attachmentRes.status === "fulfilled") {
        setAttachmentCapabilities(attachmentRes.value);
      }
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
    const epoch = ++viewEpochRef.current;
    setActiveSessionId(id);
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
  }, [detachCurrentStream, discardPendingAttachments, loadSessions, patchMsg, setSessionIndicators]);

  // 从其他功能页返回 /chat 时恢复上次查看的会话；带 ?q= 的推荐问题仍开启新对话。
  useEffect(() => {
    if (sessionsLoading || restoredSessionRef.current) return;
    restoredSessionRef.current = true;
    if (searchParams.get("q") || searchParams.get("new") === "1") return;
    const remembered = window.localStorage.getItem(LAST_CHAT_SESSION_KEY);
    if (remembered && sessions.some((session) => session.id === remembered)) {
      const timer = window.setTimeout(() => void selectSession(remembered), 0);
      return () => window.clearTimeout(timer);
    } else if (remembered) {
      window.localStorage.removeItem(LAST_CHAT_SESSION_KEY);
    }
  }, [searchParams, selectSession, sessions, sessionsLoading]);

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
    setActiveRunId(null);
    setStreaming(false);
    setMessages([]);
    setActiveCitation(null);
    setCitationMsgId(null);
    setSessionsOpen(false);
    window.localStorage.removeItem(LAST_CHAT_SESSION_KEY);
  }, [detachCurrentStream, discardPendingAttachments]);

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
          window.localStorage.removeItem(LAST_CHAT_SESSION_KEY);
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
    [activeRunId, activeSessionId, loadSessions, setSessionIndicators],
  );

  const removeAttachment = useCallback(async (attachment: ChatAttachmentOut) => {
    setAttachments((prev) => prev.filter((item) => item.id !== attachment.id));
    try {
      await apiJson<void>(`/api/chat/attachments/${attachment.id}`, {
        method: "DELETE",
      });
    } catch (err) {
      setBanner(err instanceof ApiError ? err.message : "移除附件失败");
    }
  }, []);

  const uploadChatAttachmentFiles = async (files: File[]) => {
    if (files.length === 0 || uploadingAttachment) return;
    const maxCount = attachmentCapabilities?.max_files_per_message ?? 4;
    const available = maxCount - attachments.length;
    if (available <= 0 || files.length > available) {
      setBanner(`每条消息最多添加 ${maxCount} 个附件`);
      return;
    }
    const maxBytes = (attachmentCapabilities?.max_file_mb ?? 20) * 1024 * 1024;
    if (files.some((file) => file.size > maxBytes)) {
      setBanner(`当前对话单个附件不能超过 ${attachmentCapabilities?.max_file_mb ?? 20}MB`);
      return;
    }
    const existingBytes = attachments.reduce((sum, item) => sum + item.size_bytes, 0);
    if (existingBytes + files.reduce((sum, file) => sum + file.size, 0) > maxBytes) {
      setBanner(`本次消息的附件总大小不能超过 ${attachmentCapabilities?.max_file_mb ?? 20}MB`);
      return;
    }
    setUploadingAttachment(true);
    setComposerPanel(null);
    const uploaded: ChatAttachmentOut[] = [];
    const failures: string[] = [];
    for (const file of files) {
      try {
        const form = new FormData();
        form.append("file", file);
        const response = await apiFetch("/api/chat/attachments", {
          method: "POST",
          body: form,
        });
        uploaded.push((await response.json()) as ChatAttachmentOut);
      } catch (err) {
        failures.push(
          `${file.name}：${err instanceof ApiError ? err.message : "上传失败"}`,
        );
      }
    }
    setAttachments((prev) => [...prev, ...uploaded]);
    if (uploaded.length > 0) {
      setChatMode((mode) =>
        mode === "web" ? "fast" : mode === "research" ? "think" : mode,
      );
    }
    setUploadingAttachment(false);
    setBanner(failures.length > 0 ? failures.join("；") : null);
  };

  const uploadChatAttachments = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    void uploadChatAttachmentFiles(files);
  };

  const handleComposerPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const suffixByMime: Record<string, string> = {
      "image/png": "png",
      "image/jpeg": "jpg",
      "image/webp": "webp",
    };
    const files = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null)
      .map((file, index) => {
        const suffix = suffixByMime[file.type];
        if (!suffix || file.name.includes(".")) return file;
        return new File(
          [file],
          `粘贴图片-${Date.now()}-${index + 1}.${suffix}`,
          { type: file.type, lastModified: file.lastModified },
        );
      });
    if (files.length === 0) return;
    // textarea 无法直接承载位图；图片交给附件区。若剪贴板同时有文字，仍让浏览器正常粘贴文字。
    if (!event.clipboardData.getData("text/plain")) event.preventDefault();
    void uploadChatAttachmentFiles(files);
  };

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
              window.localStorage.setItem(
                LAST_CHAT_SESSION_KEY,
                d.session_id,
              );
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
  }, [activeRunId, activeSessionId, setSessionIndicators]);

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
    [],
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
            className="absolute inset-0 bg-slate-900/40"
            onClick={() => setSessionsOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 w-72 bg-white shadow-xl">
            {renderSessionList(() => setSessionsOpen(false))}
          </div>
        </div>
      )}

      {/* 中：消息区 */}
      <section className="flex min-w-0 flex-1 flex-col bg-canvas">
        {/* 工具栏：知识库选择器 */}
        <div className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-[#fafaf9] px-5">
          {historyCollapsed && (
            <button
              type="button"
              onClick={() => setHistoryCollapsed(false)}
              className="hidden h-8 w-8 items-center justify-center rounded-lg text-ink-secondary hover:bg-surface-muted hover:text-ink md:flex"
              aria-label="展开历史对话"
              title="展开历史对话"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-[18px] w-[18px]" aria-hidden>
                <rect x="3" y="4" width="18" height="16" rx="3" />
                <path d="M9 4v16" />
              </svg>
            </button>
          )}
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

          <h1 className="min-w-0 truncate text-sm font-semibold text-ink">
            {activeSessionTitle}
          </h1>

          <span className="flex-1" />

          {panelCitations.length > 0 && (
            <button
              type="button"
              onClick={() => setCitationsOpen(true)}
              className="relative rounded-control p-1.5 text-slate-500 hover:bg-slate-100 xl:hidden"
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
              <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-indigo-600 px-1 text-caption font-semibold text-white">
                {panelCitations.length}
              </span>
            </button>
          )}
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
      </section>

      {/* 右：引用面板（桌面）—— 用户点击引用后才打开 */}
      {citationsOpen && panelCitations.length > 0 && (
      <aside className="hidden w-[318px] shrink-0 flex-col border-l border-line bg-white xl:flex">
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-slate-100 px-4">
          <h2 className="text-base font-medium text-slate-900">
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
            className="rounded-[7px] p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
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
            className="absolute inset-0 bg-slate-900/40"
            onClick={() => setCitationsOpen(false)}
          />
          <div className="absolute inset-y-0 right-0 flex w-80 max-w-[85vw] flex-col bg-white shadow-xl">
            <div className="flex h-11 shrink-0 items-center justify-between border-b border-slate-100 px-4">
              <h2 className="text-sm font-semibold text-slate-800">{citationsPanelTitle}</h2>
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
        <div className="flex h-full items-center justify-center text-sm text-slate-400">
          对话加载中…
        </div>
      }
    >
      <ChatView />
    </Suspense>
  );
}
