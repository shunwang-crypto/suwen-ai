"use client";

// 统一轻量 toast 展示层：根布局唯一挂载。普通提示自动消失，错误和警告提示
// 保持到用户明确关闭。

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  dismissToast,
  subscribeToasts,
  type ToastItem,
  type ToastType,
} from "@/lib/toast";

const AUTO_HIDE_MS: Record<ToastType, number | null> = {
  success: 4_000,
  info: 6_000,
  error: null,
  warning: null,
};

const TONE: Record<ToastType, string> = {
  success: "border-emerald-300 bg-emerald-50 text-emerald-800",
  error: "border-red-300 bg-red-50 text-red-700",
  info: "border-line-strong bg-white text-ink",
  warning: "border-amber-300 bg-amber-50 text-amber-800",
};

const ICON_TONE: Record<ToastType, string> = {
  success: "text-emerald-500",
  error: "text-red-500",
  info: "text-ink-muted",
  warning: "text-amber-500",
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
  if (type === "error" || type === "warning") {
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

function ToastCard({ item }: { item: ToastItem }) {
  const duration = item.durationMs === undefined ? AUTO_HIDE_MS[item.type] : item.durationMs;

  useEffect(() => {
    if (duration == null) return;
    const timer = window.setTimeout(() => dismissToast(item.id), duration);
    return () => window.clearTimeout(timer);
  }, [duration, item.id]);

  const urgent = item.type === "error" || item.type === "warning";

  return (
    <div
      role={urgent ? "alert" : "status"}
      aria-live={urgent ? "assertive" : "polite"}
      className={`pointer-events-auto flex max-w-full items-center gap-3 rounded-card border py-2.5 pl-4 pr-2.5 text-sm shadow-floating animate-fade-up ${TONE[item.type]}`}
    >
      <ToastGlyph type={item.type} />
      <span className="min-w-0 leading-5">{item.message}</span>
      {item.action && (
        <Link
          href={item.action.href}
          onClick={() => dismissToast(item.id)}
          className="shrink-0 rounded-control bg-amber-600 px-3 py-1.5 text-xs font-semibold text-white no-underline transition-colors hover:bg-amber-700"
        >
          {item.action.label}
        </Link>
      )}
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
  );
}

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([]);

  useEffect(() => subscribeToasts(setItems), []);

  if (items.length === 0) return null;

  return (
    <div className="pointer-events-none fixed bottom-6 left-1/2 z-[70] flex w-max max-w-[calc(100vw-2rem)] -translate-x-1/2 flex-col items-center gap-2">
      {items.map((item) => <ToastCard key={item.id} item={item} />)}
    </div>
  );
}
