"use client";

// 会话列表：新建 / 选择 / 置顶 / 重命名 / 删除。

import {
  Fragment,
  useEffect,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { createPortal } from "react-dom";

import type { SessionOut } from "@/lib/types";

const PINNED_SESSIONS_KEY = "eduagents:pinned-chat-sessions";

function readPinnedSessions(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const value = JSON.parse(
      window.localStorage.getItem(PINNED_SESSIONS_KEY) ?? "[]",
    );
    return Array.isArray(value)
      ? value.filter((id): id is string => typeof id === "string")
      : [];
  } catch {
    return [];
  }
}

export function SessionList({
  sessions,
  activeId,
  loading,
  indicators,
  onSelect,
  onNew,
  onCollapse,
  onRename,
  onDelete,
}: {
  sessions: SessionOut[];
  activeId: string | null;
  loading: boolean;
  indicators: Record<string, "running" | "completed">;
  onSelect: (id: string) => void;
  onNew: () => void;
  onCollapse: () => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (id: string) => Promise<boolean>;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [pinnedIds, setPinnedIds] = useState<string[]>(readPinnedSessions);
  const [deleteTarget, setDeleteTarget] = useState<SessionOut | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [menu, setMenu] = useState<{
    id: string;
    top: number;
    left: number;
  } | null>(null);

  useEffect(() => {
    window.localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify(pinnedIds));
  }, [pinnedIds]);

  useEffect(() => {
    if (!menu) return;
    const closeOutside = (event: PointerEvent) => {
      if (
        event.target instanceof Element &&
        event.target.closest("[data-session-menu-root]")
      ) {
        return;
      }
      setMenu(null);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setMenu(null);
    };
    const closeOnViewportChange = () => setMenu(null);
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", closeOnViewportChange);
    window.addEventListener("scroll", closeOnViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", closeOnViewportChange);
      window.removeEventListener("scroll", closeOnViewportChange, true);
    };
  }, [menu]);

  useEffect(() => {
    if (!deleteTarget) return;
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && !deleting) setDeleteTarget(null);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [deleteTarget, deleting]);

  const pinnedSet = new Set(pinnedIds);
  const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
  const visibleSessions = normalizedQuery
    ? sessions.filter((session) =>
        (session.title || "未命名会话")
          .toLocaleLowerCase("zh-CN")
          .includes(normalizedQuery),
      )
    : sessions;
  const orderedSessions = [...visibleSessions].sort(
    (a, b) => Number(pinnedSet.has(b.id)) - Number(pinnedSet.has(a.id)),
  );

  const commitRename = async (id: string) => {
    const title = draft.trim();
    setEditingId(null);
    if (title) await onRename(id, title);
  };

  const handleKey = (e: KeyboardEvent<HTMLInputElement>, id: string) => {
    if (e.key === "Enter") void commitRename(id);
    if (e.key === "Escape") setEditingId(null);
  };

  const openMenu = (event: MouseEvent<HTMLButtonElement>, id: string) => {
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    const menuWidth = 176;
    const menuHeight = 132;
    const below = rect.bottom + 6;
    setMenu({
      id,
      left: Math.max(
        8,
        Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 8),
      ),
      top:
        below + menuHeight <= window.innerHeight
          ? below
          : Math.max(8, rect.top - menuHeight - 6),
    });
  };

  const togglePinned = (id: string) => {
    setPinnedIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id],
    );
    setMenu(null);
  };

  const beginRename = (session: SessionOut) => {
    setEditingId(session.id);
    setDraft(session.title);
    setMenu(null);
  };

  const requestDelete = (session: SessionOut) => {
    setMenu(null);
    setDeleteTarget(session);
  };

  const confirmDelete = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    try {
      const deleted = await onDelete(deleteTarget.id);
      if (!deleted) return;
      setPinnedIds((prev) => prev.filter((id) => id !== deleteTarget.id));
      setDeleteTarget(null);
    } finally {
      setDeleting(false);
    }
  };

  const menuSession = menu
    ? sessions.find((session) => session.id === menu.id)
    : undefined;

  return (
    <div className="flex h-full flex-col bg-surface">
      <div className="flex h-14 shrink-0 items-center gap-1 px-3">
        <h2 className="text-sm font-semibold text-ink">历史对话</h2>
        <span className="flex-1" />
        <button
          type="button"
          onClick={() => setSearchOpen((value) => !value)}
          title="搜索对话"
          aria-label="搜索对话"
          className="flex h-8 w-8 items-center justify-center rounded-lg text-ink-secondary hover:bg-surface-muted hover:text-ink"
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            className="h-4 w-4"
            aria-hidden
          >
            <circle cx="11" cy="11" r="6.5" />
            <path d="m16 16 4 4" />
          </svg>
        </button>
        <button
          type="button"
          onClick={onCollapse}
          title="收起历史对话"
          aria-label="收起历史对话"
          className="flex h-8 w-8 items-center justify-center rounded-lg text-ink-secondary hover:bg-surface-muted hover:text-ink"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" className="h-[18px] w-[18px]" aria-hidden>
            <rect x="3" y="4" width="18" height="16" rx="3" />
            <path d="M15 4v16" />
          </svg>
        </button>
      </div>

      {searchOpen && (
        <div className="px-3 pb-2">
          <div className="flex h-9 items-center gap-2 rounded-full border border-line bg-white px-3 focus-within:border-line-strong">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-4 w-4 shrink-0 text-ink-muted" aria-hidden>
              <circle cx="11" cy="11" r="6.5" />
              <path d="m16 16 4 4" />
            </svg>
            <input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索历史对话"
              className="min-w-0 flex-1 bg-transparent text-xs text-ink outline-none placeholder:text-ink-muted focus-visible:outline-none"
            />
            {query && (
              <button type="button" onClick={() => setQuery("")} className="text-xs text-ink-muted hover:text-ink" aria-label="清空搜索">×</button>
            )}
          </div>
        </div>
      )}

      <div className="px-3 pb-3">
        <button
          type="button"
          onClick={onNew}
          className="flex h-10 w-full items-center justify-center gap-2 rounded-full border border-line bg-white text-[13px] font-medium text-ink shadow-sm transition-colors hover:border-line-strong hover:bg-surface-muted"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="h-[17px] w-[17px]" aria-hidden>
            <circle cx="12" cy="12" r="9" />
            <path d="M12 8v8M8 12h8" />
          </svg>
          开启新对话
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2.5 pb-2">
        {loading ? (
          <p className="px-3 py-2 text-sm text-slate-400">加载中…</p>
        ) : sessions.length === 0 ? (
          <p className="px-3 py-2 text-sm text-slate-400">暂无会话</p>
        ) : orderedSessions.length === 0 ? (
          <p className="px-3 py-4 text-center text-xs text-slate-400">没有找到相关对话</p>
        ) : (
          <ul className="space-y-0.5">
            {orderedSessions.map((s, index) => {
              const active = s.id === activeId;
              const indicator = indicators[s.id];
              const pinned = pinnedSet.has(s.id);
              const previousPinned =
                index > 0 && pinnedSet.has(orderedSessions[index - 1].id);
              return (
                <Fragment key={s.id}>
                  {index === 0 && pinned && (
                    <li className="px-1.5 pb-0.5 pt-1 text-[10px] font-normal text-slate-400">
                      置顶
                    </li>
                  )}
                  {!pinned && (index === 0 || previousPinned) && (
                    <li className="px-1.5 pb-0.5 pt-2 text-[10px] font-normal text-slate-400">
                      最近 7 天
                    </li>
                  )}
                  <li className="group relative">
                    {editingId === s.id ? (
                      <input
                        autoFocus
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        onBlur={() => void commitRename(s.id)}
                        onKeyDown={(e) => handleKey(e, s.id)}
                        maxLength={255}
                        className="h-9 w-full rounded-lg border border-line-strong bg-white px-2.5 text-xs text-slate-900 outline-none"
                      />
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => onSelect(s.id)}
                          className={`flex h-9 w-full items-center gap-2 rounded-lg pl-2.5 pr-2 text-left text-[12px] transition-[background-color,color,padding] group-hover:pr-9 ${
                            active
                              ? "bg-surface-muted font-medium text-ink"
                              : "text-ink-secondary hover:bg-surface-muted hover:text-ink"
                          }`}
                        >
                          {pinned && (
                            <svg
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="1.8"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              className="h-3.5 w-3.5 shrink-0 text-slate-400"
                              aria-label="已置顶"
                            >
                              <path d="M12 17v5M5 3h14M7 3l1 7-3 4h14l-3-4 1-7" />
                            </svg>
                          )}
                          <span
                            title={s.title || "未命名会话"}
                            className="min-w-0 flex-1 truncate"
                          >
                            {s.title || "未命名会话"}
                          </span>
                          <span className="flex shrink-0 items-center gap-1.5 group-hover:hidden">
                            {indicator === "running" && (
                              <span
                                role="status"
                                aria-label="正在生成回答"
                                title="正在生成回答"
                                className="h-3 w-3 animate-spin rounded-full border-2 border-brand-100 border-t-brand-600"
                              />
                            )}
                            {indicator === "completed" && (
                              <span
                                role="status"
                                aria-label="回答已完成"
                                title="回答已完成"
                                className="h-2 w-2 rounded-full bg-sky-500"
                              />
                            )}
                          </span>
                        </button>
                        <span className="absolute right-1.5 top-1/2 hidden -translate-y-1/2 group-hover:block">
                          <button
                            type="button"
                            title="会话操作"
                            aria-label={`打开“${s.title || "未命名会话"}”的操作菜单`}
                            data-session-menu-root
                            onClick={(event) => openMenu(event, s.id)}
                            className="rounded-control p-1 text-slate-400 hover:bg-slate-200 hover:text-slate-700"
                          >
                            <svg
                              viewBox="0 0 24 24"
                              fill="currentColor"
                              className="h-3.5 w-3.5"
                              aria-hidden
                            >
                              <circle cx="5" cy="12" r="1.8" />
                              <circle cx="12" cy="12" r="1.8" />
                              <circle cx="19" cy="12" r="1.8" />
                            </svg>
                          </button>
                        </span>
                      </>
                    )}
                  </li>
                </Fragment>
              );
            })}
          </ul>
        )}
      </div>

      <p className="shrink-0 border-t border-slate-200 px-3 py-2 text-[10px] text-slate-400">
        共 {sessions.length} 个对话 · 自动保存
      </p>

      {menu &&
        menuSession &&
        createPortal(
          <div
            data-session-menu-root
            role="menu"
            aria-label="会话操作"
            style={{ top: menu.top, left: menu.left }}
            className="fixed z-[70] w-44 rounded-xl border border-slate-200 bg-white p-1.5 shadow-xl shadow-slate-900/10"
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => togglePinned(menuSession.id)}
              className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100"
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
                <path d="m14 4 6 6-3 1-4 4-1 5-3-3-4 4-2-2 4-4-3-3 5-1 4-4 1-3Z" />
              </svg>
              {pinnedSet.has(menuSession.id) ? "取消置顶" : "置顶"}
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => beginRename(menuSession)}
              className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100"
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
                <path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3Z" />
              </svg>
              重命名
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => requestDelete(menuSession)}
              className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm text-red-600 hover:bg-red-50"
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
                <path d="M3 6h18m-2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
              </svg>
              删除
            </button>
          </div>,
          document.body,
        )}

      {deleteTarget &&
        createPortal(
          <div className="fixed inset-0 z-[90] flex items-center justify-center px-4">
            <button
              type="button"
              aria-label="取消删除会话"
              className="absolute inset-0 bg-black/40"
              onClick={() => {
                if (!deleting) setDeleteTarget(null);
              }}
            />
            <section
              role="dialog"
              aria-modal="true"
              aria-labelledby="delete-session-title"
              className="relative w-full max-w-[460px] rounded-[16px] border border-line bg-white p-6 shadow-floating"
            >
              <h2 id="delete-session-title" className="text-[20px] font-semibold text-ink">
                删除对话？
              </h2>
              <p className="mt-5 text-[15px] leading-6 text-ink-secondary">
                这将永久删除“
                <strong className="font-semibold text-ink">
                  {deleteTarget.title || "未命名会话"}
                </strong>
                ”以及其中的所有消息和附件。
              </p>
              <p className="mt-2 text-[13px] leading-5 text-ink-muted">
                已提取的长期学习记忆不会随对话删除，可在“账户设置 → AI 记忆”中单独管理。
              </p>
              <div className="mt-7 flex justify-end gap-2.5">
                <button
                  type="button"
                  disabled={deleting}
                  onClick={() => setDeleteTarget(null)}
                  className="h-10 rounded-full border border-line-strong bg-white px-5 text-[14px] font-medium text-ink hover:bg-surface-muted disabled:opacity-50"
                >
                  取消
                </button>
                <button
                  type="button"
                  disabled={deleting}
                  onClick={() => void confirmDelete()}
                  className="h-10 rounded-full bg-danger-600 px-5 text-[14px] font-medium text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {deleting ? "正在删除…" : "删除"}
                </button>
              </div>
            </section>
          </div>,
          document.body,
        )}
    </div>
  );
}
