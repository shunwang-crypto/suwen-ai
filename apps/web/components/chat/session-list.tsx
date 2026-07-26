"use client";

// 会话列表：新建 / 选择 / 重命名 / 删除。

import { useState, type KeyboardEvent } from "react";

import type { SessionOut } from "@/lib/types";

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) {
    return d.toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export function SessionList({
  sessions,
  activeId,
  loading,
  onSelect,
  onNew,
  onRename,
  onDelete,
}: {
  sessions: SessionOut[];
  activeId: string | null;
  loading: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const commitRename = async (id: string) => {
    const title = draft.trim();
    setEditingId(null);
    if (title) await onRename(id, title);
  };

  const handleKey = (e: KeyboardEvent<HTMLInputElement>, id: string) => {
    if (e.key === "Enter") void commitRename(id);
    if (e.key === "Escape") setEditingId(null);
  };

  return (
    <div className="flex h-full flex-col">
      <div className="p-3">
        <button
          type="button"
          onClick={onNew}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm font-medium text-indigo-700 transition-colors hover:bg-indigo-100"
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
          新建对话
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {loading ? (
          <p className="px-3 py-2 text-sm text-slate-400">加载中…</p>
        ) : sessions.length === 0 ? (
          <p className="px-3 py-2 text-sm text-slate-400">暂无会话</p>
        ) : (
          <ul className="space-y-0.5">
            {sessions.map((s) => {
              const active = s.id === activeId;
              return (
                <li key={s.id} className="group relative">
                  {editingId === s.id ? (
                    <input
                      autoFocus
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onBlur={() => void commitRename(s.id)}
                      onKeyDown={(e) => handleKey(e, s.id)}
                      maxLength={255}
                      className="w-full rounded-lg border border-indigo-400 bg-white px-3 py-2 text-sm text-slate-900 outline-none ring-2 ring-indigo-100"
                    />
                  ) : (
                    <>
                      <button
                        type="button"
                        onClick={() => onSelect(s.id)}
                        className={`flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                          active
                            ? "bg-indigo-50 text-indigo-800"
                            : "text-slate-700 hover:bg-slate-100"
                        }`}
                      >
                        <span className="min-w-0 flex-1 truncate">
                          {s.title || "未命名会话"}
                        </span>
                        <span className="shrink-0 text-xs text-slate-400 group-hover:hidden">
                          {formatTime(s.updated_at)}
                        </span>
                      </button>
                      <span className="absolute right-1.5 top-1/2 hidden -translate-y-1/2 items-center gap-0.5 group-hover:flex">
                        <button
                          type="button"
                          title="重命名"
                          aria-label="重命名会话"
                          onClick={() => {
                            setEditingId(s.id);
                            setDraft(s.title);
                          }}
                          className="rounded p-1 text-slate-400 hover:bg-slate-200 hover:text-slate-700"
                        >
                          <svg
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="1.8"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            className="h-3.5 w-3.5"
                            aria-hidden
                          >
                            <path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3Z" />
                          </svg>
                        </button>
                        <button
                          type="button"
                          title="删除"
                          aria-label="删除会话"
                          onClick={() => {
                            if (window.confirm(`删除会话「${s.title}」？该操作不可恢复。`)) {
                              void onDelete(s.id);
                            }
                          }}
                          className="rounded p-1 text-slate-400 hover:bg-red-50 hover:text-red-600"
                        >
                          <svg
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="1.8"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            className="h-3.5 w-3.5"
                            aria-hidden
                          >
                            <path d="M3 6h18m-2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                          </svg>
                        </button>
                      </span>
                    </>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
