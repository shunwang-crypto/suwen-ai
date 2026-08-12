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
import { MarkdownAnswer } from "@/components/chat/markdown-answer";
import { OpeningCard } from "@/components/chat/opening-card";
import { SessionList } from "@/components/chat/session-list";
import {
  ProcessTimeline,
  type TimelineItem,
} from "@/components/chat/timeline";
import { ApiError, apiFetch, apiJson } from "@/lib/api";
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
  /** 当前 token 流的重生成序号：变化时清空重排（防拼接双答案） */
  attempt?: number;
  /** 追问 chips（followups 事件；kb 路径 done 前下发） */
  followups?: string[];
  /** 发送时绑定的知识库（「考考我」chip 跳转 /quiz 用） */
  kbId?: string;
  kbName?: string;
  /** 回答未完成（断流/手动停止；历史消息读 meta.truncated）→ 角标 + 重问入口 */
  truncated?: boolean;
  /** 45s 无事件软超时（客户端合成 stall 事件）：仍在等待上游 */
  stalled?: boolean;
  attachments?: ChatAttachmentOut[];
  chatMode?: ChatMode;
}

type ChatMode = "fast" | "think" | "web" | "research";

/** done.usage.mode 中文化（P2-18）：内部枚举不直接暴露给用户 */
const MODE_LABELS: Record<string, string> = {
  cache: "缓存命中",
  replay: "离线回放",
  live: "真实调用",
  record: "真实调用",
  stub: "演示模式",
};

const SAMPLE_QUESTIONS = [
  "用泰勒展开解释 $e^x$ 的近似计算",
  "什么是贝叶斯定理？举一个生活中的例子",
  "帮我梳理二次函数 $y=ax^2+bx+c$ 的图像性质",
];

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
                {/* F8-A2 主动开场卡：仅新会话（消息为空）首屏；有消息即随本分支隐藏 */}
                <OpeningCard />
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
                              <MarkdownAnswer
                                text={m.content}
                                citationIndexes={m.citations.map((citation) => citation.idx)}
                                onCitationClick={(idx) =>
                                  handleCitationClick(m.id, idx)
                                }
                                activeCitation={
                                  citedMsg?.id === m.id ? activeCitation : null
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
                        <div className="mt-3 flex items-center gap-2 text-xs text-slate-400">
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
                        </div>
                      )}
                    </div>
                  </div>
                ),
              )
            )}
          </div>
        </div>

        {/* 输入区 */}
        <div className="shrink-0 bg-canvas px-4 pb-6 pt-3">
          <div
            className={`relative mx-auto flex w-full max-w-[720px] flex-col border border-line bg-white px-2 py-2 shadow-subtle transition-[border-color,border-radius] focus-within:border-line-strong ${
              attachments.length > 0 || kbIds.length > 0 || composerMultiline
                ? "rounded-[24px]"
                : "rounded-full"
            }`}
          >
            <input
              ref={chatAttachmentInputRef}
              type="file"
              multiple
              accept={[
                ...(attachmentCapabilities?.document_suffixes ?? [
                  ".pdf",
                  ".docx",
                  ".pptx",
                  ".md",
                  ".txt",
                ]),
                ...(attachmentCapabilities?.image_suffixes ?? []),
              ].join(",")}
              className="hidden"
              onChange={(event) => void uploadChatAttachments(event)}
            />

            {(attachments.length > 0 || kbIds.length > 0) && (
              <div className="flex flex-wrap gap-1.5 px-2 pb-2 pt-1">
                {kbIds.length > 0 && (
                  <span className="inline-flex max-w-64 items-center gap-1.5 rounded-[7px] border border-line bg-surface-muted px-2 py-1 text-body-sm text-ink-secondary">
                    <button
                      type="button"
                      onClick={() => setComposerPanel("scope")}
                      className="max-w-52 truncate hover:text-ink"
                      title={selectedKbLabel}
                    >
                      知识库 · {selectedKbLabel}
                    </button>
                    <button
                      type="button"
                      onClick={() => setKbIds([])}
                      className="text-ink-muted hover:text-ink"
                      aria-label="清除知识库范围"
                    >
                      ×
                    </button>
                  </span>
                )}
                {attachments.map((attachment) => (
                  <span
                    key={attachment.id}
                    className="inline-flex max-w-64 items-center gap-1.5 rounded-[7px] border border-line bg-surface-muted px-2 py-1 text-body-sm text-ink-secondary"
                  >
                    <span aria-hidden>
                      {attachment.kind === "image" ? "图片" : "文件"}
                    </span>
                    <span className="truncate" title={attachment.filename}>
                      {attachment.filename}
                    </span>
                    <button
                      type="button"
                      onClick={() => void removeAttachment(attachment)}
                      className="shrink-0 text-ink-muted hover:text-ink"
                      aria-label={`移除 ${attachment.filename}`}
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
            )}

            <div className="flex min-h-10 items-end gap-1.5">
              <div className="relative">
                <button
                  type="button"
                  onClick={() =>
                    setComposerPanel((value) => (value ? null : "menu"))
                  }
                  disabled={streaming || uploadingAttachment}
                  className="flex h-10 w-10 items-center justify-center rounded-full text-2xl font-light text-ink-secondary transition-colors hover:bg-surface-muted disabled:opacity-40"
                  aria-label="添加内容"
                  aria-expanded={composerPanel === "menu"}
                >
                  {uploadingAttachment ? (
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700" />
                  ) : (
                    "+"
                  )}
                </button>
              </div>

              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleComposerKey}
                onPaste={handleComposerPaste}
                rows={1}
                maxLength={8000}
                placeholder={streaming ? "正在生成回答…" : "给苏问发消息"}
                disabled={streaming}
                className="chat-composer-input my-2 min-h-6 min-w-0 flex-1 resize-none border-0 bg-transparent p-0 text-[15px] leading-6 text-slate-900 outline-none placeholder:text-slate-400 focus-visible:outline-none disabled:text-slate-400"
              />

              <div className="relative mb-0.5 shrink-0">
                <button
                  type="button"
                  onClick={() =>
                    setComposerPanel((value) => (value === "mode" ? null : "mode"))
                  }
                  disabled={streaming}
                  className="flex h-9 max-w-[190px] items-center gap-1.5 rounded-full px-2.5 text-xs font-medium text-ink-secondary transition-colors hover:bg-surface-muted disabled:opacity-40"
                  aria-label="选择回答模式"
                  aria-expanded={composerPanel === "mode" || composerPanel === "scope"}
                  title={selectedModeLabel}
                >
                  <span className="truncate">{selectedModeLabel}</span>
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-3.5 w-3.5 shrink-0" aria-hidden>
                    <path d="m5 6 3 3 3-3" />
                  </svg>
                </button>
              </div>

              {streaming ? (
                <button
                  type="button"
                  onClick={stop}
                  title="停止生成"
                  aria-label="停止生成"
                  className="mb-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-slate-300 text-slate-600 transition-colors hover:bg-slate-100"
                >
                  <span className="h-2.5 w-2.5 rounded-sm bg-slate-500" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void send()}
                  disabled={!input.trim() && attachments.length === 0}
                  title="发送"
                  aria-label="发送"
                  className="mb-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-brand-600 text-white transition-colors hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <svg
                    viewBox="0 0 16 16"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-4 w-4"
                    aria-hidden
                  >
                    <path d="M8 12V4M4.5 7.5 8 4l3.5 3.5" />
                  </svg>
                </button>
              )}
            </div>

            {composerPanel && (
              <>
                <button
                  type="button"
                  className="fixed inset-0 z-20 cursor-default"
                  aria-label="关闭输入菜单"
                  onClick={() => setComposerPanel(null)}
                />

                {composerPanel === "menu" && (
                  <div className="absolute bottom-[58px] left-0 z-30 w-[280px] rounded-2xl border border-line bg-white p-2 shadow-floating">
                    <button
                      type="button"
                      onClick={() => chatAttachmentInputRef.current?.click()}
                      className="flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-surface-muted"
                    >
                      <span className="mt-0.5 flex h-7 w-7 items-center justify-center rounded-lg border border-line text-lg" aria-hidden>＋</span>
                      <span>
                        <span className="block text-[13px] font-medium text-ink">添加图片和文件</span>
                        <span className="block text-body-sm leading-5 text-ink-muted">选择文件，或在输入框直接粘贴截图</span>
                        <span className="block text-caption leading-4 text-ink-muted">最多 4 个，合计 20MB</span>
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => setComposerPanel("scope")}
                      className="flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-surface-muted"
                    >
                      <span className="mt-0.5 flex h-7 w-7 items-center justify-center rounded-lg border border-line text-xs" aria-hidden>▤</span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-[13px] font-medium text-ink">选择知识库</span>
                        <span className="block truncate text-body-sm leading-5 text-ink-muted">
                          {kbIds.length > 0 ? `当前：${selectedKbLabel}` : "基于指定资料回答"}
                        </span>
                      </span>
                    </button>
                  </div>
                )}

                {composerPanel === "mode" && (
                  <div className="absolute bottom-[58px] right-0 z-30 w-[300px] rounded-2xl border border-line bg-white p-2 shadow-floating">
                    <p className="px-3 pb-1.5 pt-1 text-body-sm font-medium text-ink-muted">回答方式</p>
                    {([
                      ["fast", "极速", "快速直接回答"],
                      ["think", "深度思考", "用于复杂推理与综合分析"],
                      ["web", "网页搜索", "搜索实时信息并标注来源"],
                      ["research", "深度搜索", "深度思考并联网检索"],
                    ] as const).map(([mode, title, description]) => {
                      const disabled =
                        ((mode === "think" || mode === "research") &&
                          !attachmentCapabilities?.thinking_enabled) ||
                        ((mode === "web" || mode === "research") &&
                          (!attachmentCapabilities?.web_search_enabled ||
                            attachments.length > 0));
                      return (
                        <button
                          key={mode}
                          type="button"
                          disabled={disabled}
                          onClick={() => {
                            setChatMode(mode);
                            if (mode === "web" || mode === "research") setKbIds([]);
                            setComposerPanel(null);
                            setBanner(null);
                          }}
                          className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-surface-muted disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-surface-muted text-xs text-ink-secondary" aria-hidden>
                            {mode === "fast"
                              ? "↗"
                              : mode === "think"
                                ? "◇"
                                : mode === "web"
                                  ? "◎"
                                  : "✦"}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block text-[13px] font-medium text-ink">{title}</span>
                            <span className="block text-body-sm leading-5 text-ink-muted">
                              {(mode === "web" || mode === "research") &&
                              attachments.length > 0
                                ? "移除附件后可使用"
                                : disabled
                                  ? "当前部署未配置"
                                  : description}
                            </span>
                          </span>
                          {chatMode === mode && <span className="text-sm text-brand-700">✓</span>}
                        </button>
                      );
                    })}
                  </div>
                )}

                {composerPanel === "scope" && (
                  <div className="absolute bottom-[58px] left-0 z-30 max-h-[380px] w-[310px] overflow-y-auto rounded-2xl border border-line bg-white p-2 shadow-floating">
                    <button
                      type="button"
                      onClick={() => setComposerPanel("menu")}
                      className="mb-1 px-2 py-1 text-xs text-ink-muted hover:text-ink"
                    >
                      ← 选择知识库
                    </button>
                    {kbs.length === 0 ? (
                      <p className="px-3 py-5 text-center text-xs leading-5 text-ink-muted">还没有可用的知识库，请先在知识库页面创建并上传资料。</p>
                    ) : (
                      (["user", "org"] as const).map((ownerType) => {
                        const group = kbs.filter((kb) => kb.owner_type === ownerType);
                        if (group.length === 0) return null;
                        return (
                          <div key={ownerType} className="mt-1">
                            <p className="px-2.5 py-1 text-caption text-ink-muted">{ownerType === "user" ? "个人知识库" : "机构知识库"}</p>
                            {group.map((kb) => (
                              <label key={kb.id} className="flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-[13px] text-ink-secondary hover:bg-surface-muted">
                                <input
                                  type="checkbox"
                                  checked={kbIds.includes(kb.id)}
                                  onChange={(event) => {
                                    setKbIds((prev) =>
                                      event.target.checked
                                        ? [...prev, kb.id]
                                        : prev.filter((id) => id !== kb.id),
                                    );
                                    if (event.target.checked) {
                                      if (chatMode === "web") setChatMode("fast");
                                      if (chatMode === "research") setChatMode("think");
                                    }
                                  }}
                                  className="h-3.5 w-3.5 accent-slate-900"
                                />
                                <span className="min-w-0 flex-1 truncate">{kb.name}</span>
                              </label>
                            ))}
                          </div>
                        );
                      })
                    )}
                    <div className="mt-2 flex gap-2">
                      {kbIds.length > 0 && (
                        <button
                          type="button"
                          onClick={() => {
                            setKbIds([]);
                            setComposerPanel(null);
                          }}
                          className="h-9 flex-1 rounded-lg border border-line text-xs text-ink-secondary hover:bg-surface-muted"
                        >
                          清除范围
                        </button>
                      )}
                      <button
                        type="button"
                        disabled={kbIds.length === 0}
                        onClick={() => {
                          if (chatMode === "web") setChatMode("fast");
                          if (chatMode === "research") setChatMode("think");
                          setComposerPanel(null);
                          setBanner(null);
                        }}
                        className="h-9 flex-1 rounded-lg bg-ink text-xs font-medium text-white disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        使用所选资料
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
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
