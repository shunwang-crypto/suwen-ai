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
  onRename,
  onDelete,
}: {
  sessions: SessionOut[];
  activeId: string | null;
  loading: boolean;
  indicators: Record<string, "running" | "completed">;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pinnedIds, setPinnedIds] = useState<string[]>(readPinnedSessions);
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

  const pinnedSet = new Set(pinnedIds);
  const orderedSessions = [...sessions].sort(
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

  const confirmDelete = async (session: SessionOut) => {
    setMenu(null);
    if (!window.confirm(`删除会话「${session.title}」？该操作不可恢复。`))
      return;
    await onDelete(session.id);
    setPinnedIds((prev) => prev.filter((id) => id !== session.id));
  };

  const menuSession = menu
    ? sessions.find((session) => session.id === menu.id)
    : undefined;

  return (
    <div className="flex h-full flex-col bg-surface">
      <div className="flex h-[62px] shrink-0 items-center gap-2 px-4">
        <h2 className="text-sm font-semibold text-ink">会话</h2>
        <span className="flex-1" />
        <button
          type="button"
          onClick={onNew}
          title="新建对话"
          aria-label="新建对话"
          className="flex h-8 w-8 items-center justify-center rounded-control border border-line text-ink-secondary transition-colors hover:border-line-strong hover:text-ink"
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
            <path d="M12 5v14M5 12h14" />
          </svg>
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {loading ? (
          <p className="px-3 py-2 text-sm text-slate-400">加载中…</p>
        ) : sessions.length === 0 ? (
          <p className="px-3 py-2 text-sm text-slate-400">暂无会话</p>
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
                    <li className="px-1 pb-0.5 pt-1 text-xs font-normal text-slate-400">
                      置顶
                    </li>
                  )}
                  {!pinned && (index === 0 || previousPinned) && (
                    <li className="px-1 pb-0.5 pt-2 text-xs font-normal text-slate-400">
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
                        className="h-11 w-full rounded-control border border-brand-400 bg-white px-3 text-sm text-slate-900 outline-none ring-2 ring-brand-100"
                      />
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => onSelect(s.id)}
                          className={`flex h-12 w-full items-center gap-2 rounded-control py-2 pl-3 pr-2 text-left text-[13px] transition-[background-color,color,padding] group-hover:pr-9 ${
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

      <p className="shrink-0 border-t border-slate-200 px-4 py-3 text-xs text-slate-400">
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
              onClick={() => void confirmDelete(menuSession)}
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
    </div>
  );
}
