"use client";

// 六个功能页共用的三态组件与格式化工具（视觉基准同 chat 页：白底/靛蓝/圆角卡片）。

import type { ReactNode } from "react";

import type { MasteryChange } from "@/lib/types";

/** 旋转加载指示器。 */
export function Spinner({ className }: { className?: string }) {
  return (
    <span
      className={`inline-block animate-spin rounded-full border-2 border-indigo-200 border-t-indigo-600 ${
        className ?? "h-4 w-4"
      }`}
      aria-label="加载中"
    />
  );
}

/** 整块区域加载态。 */
export function LoadingState({ text = "加载中…" }: { text?: string }) {
  return (
    <div className="flex flex-col items-center gap-3 py-16 text-slate-400">
      <Spinner className="h-8 w-8" />
      <span className="text-sm">{text}</span>
    </div>
  );
}

/** 错误提示条（ApiError.message 直显），可选重试。 */
export function ErrorBanner({
  message,
  onRetry,
  onClose,
}: {
  message: string;
  onRetry?: () => void;
  onClose?: () => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700">
      <span className="min-w-0 break-words">{message}</span>
      <span className="flex shrink-0 items-center gap-2">
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="rounded-lg border border-red-300 px-2.5 py-1 text-xs font-medium text-red-700 hover:bg-red-100"
          >
            重试
          </button>
        )}
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="text-red-400 hover:text-red-600"
            aria-label="关闭提示"
          >
            ✕
          </button>
        )}
      </span>
    </div>
  );
}

/** 空态占位。 */
export function EmptyState({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
      <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-indigo-50 text-indigo-400">
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-5.5 w-5.5"
          aria-hidden
        >
          <path d="M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-4v4m0 4h.01" />
        </svg>
      </span>
      <h3 className="text-sm font-semibold text-slate-800">{title}</h3>
      {description && <p className="text-xs text-slate-500">{description}</p>}
      {children}
    </div>
  );
}

/** 白底圆角卡片容器。 */
export function Card({
  title,
  extra,
  children,
  className,
}: {
  title?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-2xl border border-slate-200 bg-white ${className ?? ""}`}
    >
      {(title || extra) && (
        <header className="flex items-center justify-between gap-2 border-b border-slate-100 px-4 py-2.5">
          <h2 className="text-sm font-semibold text-slate-800">{title}</h2>
          {extra}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

// ---- 格式化 ----

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

export function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

export function fmtPct(p: number): string {
  return `${Math.round(p * 100)}%`;
}

/** 掌握度变化小徽章：old → new（升绿降红）。 */
export function MasteryDelta({
  change,
  label,
}: {
  change: MasteryChange;
  label?: string;
}) {
  const up = change.new_p >= change.old_p;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${
        up ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"
      }`}
      title={`知识点 ${change.kc_id}`}
    >
      <span className="max-w-28 truncate">
        {label ?? `KC ${change.kc_id.slice(0, 6)}`}
      </span>
      <span className="font-mono">
        {change.old_p.toFixed(2)}→{change.new_p.toFixed(2)}
      </span>
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={`h-3 w-3 ${up ? "" : "rotate-180"}`}
        aria-hidden
      >
        <path d="M12 19V5m-6 6 6-6 6 6" />
      </svg>
    </span>
  );
}
