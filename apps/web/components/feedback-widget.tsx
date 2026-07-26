"use client";

// 用户反馈悬浮入口（P2-1）：全局右下角「反馈」按钮 → 弹窗表单（分类 + 描述），
// 自动附带当前页面路径与最近一次 API 响应的 X-Request-ID（报障可与后端日志串联）。
// 主线接线（一行，app/layout.tsx 的 AuthProvider 内）：<FeedbackWidget />

import { useCallback, useEffect, useState } from "react";

import { ApiError, apiJson } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { FeedbackCategory, FeedbackListResponse, FeedbackOut } from "@/lib/types";

export const FEEDBACK_CATEGORY_LABELS: Record<FeedbackCategory, string> = {
  bug: "功能故障",
  answer_wrong: "AI 回答/判分有误",
  abuse: "不当内容",
  other: "其他",
};

// ---- 最近一次 X-Request-ID 捕获 ----
// lib/api.ts 归其他工作流所有，这里自包含地包一层全局 fetch 只读响应头；
// 主线若愿意可改为在 apiFetch 里记录一行并删除此包装。
let lastRequestId = "";

declare global {
  interface Window {
    __eduFeedbackRidPatched?: boolean;
  }
}

if (typeof window !== "undefined" && !window.__eduFeedbackRidPatched) {
  window.__eduFeedbackRidPatched = true; // HMR/重复挂载防双包
  const origFetch = window.fetch.bind(window);
  window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const res = await origFetch(input, init);
    try {
      const rid = res.headers.get("X-Request-ID");
      if (rid) lastRequestId = rid;
    } catch {
      // opaque 响应等读不到头的场景：忽略
    }
    return res;
  }) as typeof fetch;
}

const STATUS_LABELS: Record<string, string> = {
  open: "处理中",
  closed: "已关闭",
};

function MyFeedbackList() {
  const [items, setItems] = useState<FeedbackOut[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiJson<FeedbackListResponse>("/api/feedback");
        if (!alive) return;
        setItems(res.items);
        setError(null);
      } catch (err) {
        if (!alive) return;
        setItems([]);
        setError(err instanceof ApiError ? err.message : "反馈记录加载失败");
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (items === null) {
    return <p className="py-4 text-center text-xs text-slate-400">加载中…</p>;
  }
  if (error) {
    return <p className="py-4 text-center text-xs text-red-500">{error}</p>;
  }
  if (items.length === 0) {
    return <p className="py-4 text-center text-xs text-slate-400">还没有提交过反馈</p>;
  }
  return (
    <ul className="max-h-64 space-y-2 overflow-y-auto pr-1">
      {items.map((it) => (
        <li key={it.id} className="rounded-xl border border-slate-200 px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium text-slate-700">
              {FEEDBACK_CATEGORY_LABELS[it.category as FeedbackCategory] ?? it.category}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                it.status === "closed"
                  ? "bg-slate-100 text-slate-500"
                  : "bg-amber-50 text-amber-700"
              }`}
            >
              {STATUS_LABELS[it.status] ?? it.status}
            </span>
          </div>
          <p className="mt-1 break-words text-xs text-slate-600">{it.content}</p>
          <p className="mt-1 text-[10px] text-slate-400">
            {new Date(it.created_at).toLocaleString("zh-CN")}
            {it.page_url && ` · ${it.page_url}`}
          </p>
        </li>
      ))}
    </ul>
  );
}

export function FeedbackWidget() {
  const { status } = useAuth();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"submit" | "mine">("submit");
  const [category, setCategory] = useState<FeedbackCategory>("bug");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const submit = useCallback(async () => {
    const value = content.trim();
    if (!value || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiJson("/api/feedback", {
        body: {
          category,
          content: value,
          page_url: window.location.pathname + window.location.search,
          request_id: lastRequestId,
        },
      });
      setDone(true);
      setContent("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "提交失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }, [category, content, busy]);

  // 仅登录态展示（/login /register 与游客态不打扰）
  if (status !== "authed") return null;

  return (
    <>
      {/* 悬浮按钮：右下角，避开移动端底部安全区 */}
      <button
        type="button"
        onClick={() => {
          setOpen((v) => !v);
          setDone(false);
          setError(null);
        }}
        aria-label="提交反馈"
        className="fixed bottom-5 right-5 z-40 flex items-center gap-1.5 rounded-full bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white shadow-lg shadow-indigo-600/25 transition-colors hover:bg-indigo-700"
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
          <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z" />
        </svg>
        反馈
      </button>

      {open && (
        <div className="fixed bottom-20 right-5 z-40 w-[min(22rem,calc(100vw-2.5rem))] rounded-2xl border border-slate-200 bg-white p-4 shadow-xl">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex gap-1 rounded-xl bg-slate-100 p-1">
              {(
                [
                  ["submit", "提交反馈"],
                  ["mine", "我的反馈"],
                ] as const
              ).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setTab(key)}
                  className={`rounded-lg px-3 py-1 text-xs font-semibold transition-colors ${
                    tab === key
                      ? "bg-white text-indigo-700 shadow-sm"
                      : "text-slate-500 hover:text-slate-700"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="关闭反馈弹窗"
              className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            >
              ✕
            </button>
          </div>

          {tab === "mine" ? (
            <MyFeedbackList key={String(open)} />
          ) : done ? (
            <div className="py-6 text-center">
              <p className="text-sm font-semibold text-emerald-700">已收到你的反馈</p>
              <p className="mt-1 text-xs text-slate-500">
                我们会尽快处理；进展可在「我的反馈」中查看。
              </p>
              <button
                type="button"
                onClick={() => setDone(false)}
                className="mt-3 rounded-xl border border-slate-300 px-3.5 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-100"
              >
                再提一条
              </button>
            </div>
          ) : (
            <div className="space-y-2.5">
              <div className="flex flex-wrap gap-1.5">
                {(Object.keys(FEEDBACK_CATEGORY_LABELS) as FeedbackCategory[]).map(
                  (key) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setCategory(key)}
                      aria-pressed={category === key}
                      className={`rounded-full border px-2.5 py-1 text-xs font-medium transition-colors ${
                        category === key
                          ? "border-indigo-500 bg-indigo-50 text-indigo-700"
                          : "border-slate-200 text-slate-500 hover:border-slate-300"
                      }`}
                    >
                      {FEEDBACK_CATEGORY_LABELS[key]}
                    </button>
                  ),
                )}
              </div>
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                rows={4}
                maxLength={4000}
                placeholder="描述你遇到的问题（如 AI 答错了什么、哪里点不动）…"
                className="w-full resize-none rounded-xl border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
              />
              {error && <p className="text-xs text-red-600">{error}</p>}
              <div className="flex items-center justify-between gap-2">
                <p className="min-w-0 truncate text-[10px] text-slate-400">
                  将附带页面路径{lastRequestId ? "与最近请求 ID" : ""}，便于排查
                </p>
                <button
                  type="button"
                  disabled={busy || !content.trim()}
                  onClick={() => void submit()}
                  className="shrink-0 rounded-xl bg-indigo-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {busy ? "提交中…" : "提交"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );
}
