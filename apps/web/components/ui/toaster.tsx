"use client";

// 统一轻量 toast 展示层：订阅 lib/toast 的模块级事件并渲染。零第三方依赖，
// 类名与 components/ui/credits-toast.tsx 对齐（rounded-card / shadow-floating / animate-fade-up）。
// 挂在 AppShell 顶层全局生效；success/info 4s、error 6s 自动消失，可手动关闭。
// role="status" 无障碍播报；prefers-reduced-motion 由 globals.css 全局降级。

import { useEffect, useRef, useState } from "react";

import {
  dismissToast,
  subscribeToasts,
  type ToastItem,
  type ToastType,
} from "@/lib/toast";

const AUTO_HIDE_MS: Record<ToastType, number> = {
  success: 4_000,
  info: 4_000,
  error: 6_000,
};

const TONE: Record<ToastType, string> = {
  success: "border-emerald-300 bg-emerald-50 text-emerald-800",
  error: "border-red-300 bg-red-50 text-red-700",
  info: "border-line-strong bg-white text-ink",
};

const ICON_TONE: Record<ToastType, string> = {
  success: "text-emerald-500",
  error: "text-red-500",
  info: "text-ink-muted",
};

function ToastGlyph({ type }: { type: ToastType }) {
  const cls = `h-4.5 w-4.5 shrink-0 ${ICON_TONE[type]}`;
  if (type === "success") {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={cls}
        aria-hidden
      >
        <path d="M20 6 9 17l-5-5" />
      </svg>
    );
  }
  if (type === "error") {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={cls}
        aria-hidden
      >
        <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0ZM12 9v4m0 4h.01" />
      </svg>
    );
  }
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cls}
      aria-hidden
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16v-4m0-4h.01" />
    </svg>
  );
}

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([]);
  const scheduled = useRef<Set<number>>(new Set());

  useEffect(() => subscribeToasts(setItems), []);

  // 每条 toast 只挂一次自动消失定时器（用 ref 去重，避免新 toast 进来重置旧 toast 的计时）
  useEffect(() => {
    for (const item of items) {
      if (scheduled.current.has(item.id)) continue;
      scheduled.current.add(item.id);
      setTimeout(() => {
        dismissToast(item.id);
        scheduled.current.delete(item.id);
      }, AUTO_HIDE_MS[item.type]);
    }
  }, [items]);

  if (items.length === 0) return null;

  return (
    <div className="pointer-events-none fixed bottom-6 left-1/2 z-[60] flex w-max max-w-[calc(100vw-2rem)] -translate-x-1/2 flex-col items-center gap-2">
      {items.map((item) => (
        <div
          key={item.id}
          role="status"
          aria-live="polite"
          className={`pointer-events-auto flex items-center gap-3 rounded-card border py-2.5 pl-4 pr-2.5 text-sm shadow-floating animate-fade-up ${TONE[item.type]}`}
        >
          <ToastGlyph type={item.type} />
          <span className="min-w-0 leading-5">{item.message}</span>
          <button
            type="button"
            aria-label="关闭提示"
            onClick={() => dismissToast(item.id)}
            className="shrink-0 rounded-control p-1 opacity-60 transition hover:bg-black/5 hover:opacity-100"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              className="h-3.5 w-3.5"
              aria-hidden
            >
              <path d="M6 6l12 12M18 6 6 18" />
            </svg>
          </button>
        </div>
      ))}
    </div>
  );
}
