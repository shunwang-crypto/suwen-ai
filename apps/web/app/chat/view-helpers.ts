// 对话页（app/chat/page.tsx）的纯辅助层：存储键/编解码、消息构建与引用编号压缩。
// 与组件解耦，无 React 依赖，供 page 与 chat 相关 hooks 复用。

import { ChatMsg } from "@/components/chat/chat-msg";
import type { StorageCodec } from "@/lib/use-hydrated-local-storage";
import type {
  ChatAttachmentOut,
  CitationData,
  MessageOut,
} from "@/lib/types";

export const LAST_CHAT_SESSION_KEY = "eduagents:last-chat-session";
export const CHAT_SESSION_STATUS_KEY = "eduagents:chat-session-status";
export const CHAT_HISTORY_COLLAPSED_KEY = "suwen:chat-history-collapsed";

export type SessionIndicator = "running" | "completed";

export const sessionIndicatorsCodec: StorageCodec<
  Record<string, SessionIndicator>
> = {
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

export const EMPTY_SESSION_INDICATORS: Record<string, SessionIndicator> = {};

export function genId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const INLINE_CITATION_RE = /\[(\d{1,3})\]/g;

/** 按正文首次引用顺序压缩编号，同时保留未引用的检索结果供核查。 */
export function normalizeAnswerCitations(
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

export function historyToMessages(history: MessageOut[]): ChatMsg[] {
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

export function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
