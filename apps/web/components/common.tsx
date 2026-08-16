"use client";

// 六个功能页共用的三态组件与格式化工具（视觉基准同 chat 页：白底/靛蓝/圆角卡片）。
// 视觉升级第三路新增：FxStyles 微交互 keyframes / PageHeader 统一页头 /
// Skeleton shimmer 骨架 / 空态内联插画 / fmtRelative 友好相对时间。
// 注：Skeleton 与空态为本路局部实现，合流后可替换为第一路 components/ui/* 的
// tokens/Skeleton/EmptyState（API 对齐即可平移）。

import type { ReactNode } from "react";

import type { MasteryChange } from "@/lib/types";

// ---- 微交互 keyframes（React 19 style 提升 + href 去重；全部动效带 reduced-motion 降级） ----

const FX_CSS = `
@keyframes edu-fade-up { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
@keyframes edu-shake { 10%,90% { transform: translateX(-1px); } 20%,80% { transform: translateX(2px); } 30%,50%,70% { transform: translateX(-3px); } 40%,60% { transform: translateX(3px); } }
@keyframes edu-pop { 0% { transform: scale(.4); opacity: 0; } 60% { transform: scale(1.15); } 100% { transform: scale(1); opacity: 1; } }
@keyframes edu-pulse-hl { 0%,100% { box-shadow: 0 0 0 0 rgba(245,158,11,0); } 50% { box-shadow: 0 0 0 6px rgba(245,158,11,.30); } }
@keyframes edu-breathe { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: .4; transform: scale(.75); } }
@keyframes edu-flow-x { from { background-position: 200% 0; } to { background-position: -200% 0; } }
@keyframes edu-draw { to { stroke-dashoffset: 0; } }
@keyframes edu-strike { from { width: 0; } to { width: 100%; } }
.edu-fade-up { animation: edu-fade-up .5s cubic-bezier(.22,1,.36,1) both; }
.edu-shake { animation: edu-shake .5s ease both; }
.edu-pop { animation: edu-pop .45s cubic-bezier(.34,1.56,.64,1) both; }
.edu-pulse-hl { animation: edu-pulse-hl .9s ease-in-out 2; }
.edu-breathe { animation: edu-breathe 1.6s ease-in-out infinite; }
.edu-flow-x { background-size: 200% 100%; animation: edu-flow-x 1.3s linear infinite; }
.edu-draw { stroke-dasharray: 30; stroke-dashoffset: 30; animation: edu-draw .5s ease-out .1s forwards; }
@media (prefers-reduced-motion: reduce) {
  .edu-fade-up, .edu-shake, .edu-pop, .edu-pulse-hl, .edu-breathe, .edu-flow-x { animation: none; }
  .edu-fade-up, .edu-pop { opacity: 1; transform: none; }
  .edu-draw { animation: none; stroke-dashoffset: 0; }
}
`;

/** 微交互样式（渲染多次自动去重）。Card/PageHeader 已内置，无需手动挂载。 */
export function FxStyles() {
  return (
    <style href="edu-fx" precedence="default">
      {FX_CSS}
    </style>
  );
}

/** 统一页头：大标题 + 一句副标 + 右侧主操作按钮。 */
export function PageHeader({
  title,
  subtitle,
  action,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <FxStyles />
      <div className="min-w-0">
        <h2 className="text-2xl font-semibold tracking-[-0.03em] text-ink">
          {title}
        </h2>
        {subtitle && (
          <p className="mt-1.5 text-xs leading-relaxed text-ink-secondary">{subtitle}</p>
        )}
      </div>
      {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
    </header>
  );
}

// ---- Shimmer 骨架（全仓唯一实现; 历史 ui/skeleton 死副本已删除） ----

/** 单块 shimmer 骨架。 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={`animate-pulse rounded-lg bg-slate-200/70 ${className ?? "h-4 w-full"}`}
    />
  );
}

/** 列表骨架：N 行「圆点 + 双行文本」。 */
export function SkeletonList({ rows = 4 }: { rows?: number }) {
  return (
    <div role="status" aria-label="加载中" className="space-y-3 py-1">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3">
          <Skeleton className="h-9 w-9 shrink-0 rounded-full" />
          <div className="min-w-0 flex-1 space-y-1.5">
            <Skeleton className="h-3.5 w-3/5" />
            <Skeleton className="h-2.5 w-2/5" />
          </div>
        </div>
      ))}
      <span className="sr-only">加载中…</span>
    </div>
  );
}

/** 整页/整卡骨架：标题条 + 大块 + 双列块。 */
export function SkeletonPage() {
  return (
    <div role="status" aria-label="加载中" className="space-y-4">
      <div className="space-y-2">
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-3 w-64 max-w-full" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-3 rounded-2xl border border-slate-200 bg-white p-4">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-48 w-full rounded-xl" />
        </div>
        <div className="rounded-2xl border border-slate-200 bg-white p-4">
          <Skeleton className="mb-3 h-4 w-24" />
          <SkeletonList rows={5} />
        </div>
      </div>
      <span className="sr-only">加载中…</span>
    </div>
  );
}

// ---- 空态内联插画（自绘双色几何风，无外部资源） ----

type IllustrationName =
  | "notebook" // 错题本：干净的笔记本 + 对勾
  | "calendar" // 复习：日历 + 咖啡
  | "team" // 机构/小组：三人小组
  | "chart" // 画像/报告：图表
  | "target" // 练习：靶心
  | "map" // 计划：路线
  | "docs" // 知识库：文档
  | "chat"; // 辅导：对话气泡

const ILLUSTRATION_PATHS: Record<IllustrationName, ReactNode> = {
  notebook: (
    <>
      <rect x="22" y="14" width="52" height="68" rx="6" fill="#e0e7ff" />
      <rect x="30" y="8" width="52" height="68" rx="6" fill="#fff" stroke="#6366f1" strokeWidth="2.5" />
      <path d="M40 24h32M40 34h32M40 44h20" stroke="#c7d2fe" strokeWidth="3" strokeLinecap="round" />
      <circle cx="72" cy="66" r="15" fill="#10b981" />
      <path d="m65 66 5 5 9-10" stroke="#fff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </>
  ),
  calendar: (
    <>
      <rect x="14" y="18" width="56" height="54" rx="6" fill="#fff" stroke="#6366f1" strokeWidth="2.5" />
      <path d="M14 32h56" stroke="#6366f1" strokeWidth="2.5" />
      <path d="M28 12v10M56 12v10" stroke="#6366f1" strokeWidth="2.5" strokeLinecap="round" />
      <rect x="24" y="40" width="8" height="8" rx="2" fill="#c7d2fe" />
      <rect x="38" y="40" width="8" height="8" rx="2" fill="#c7d2fe" />
      <rect x="52" y="40" width="8" height="8" rx="2" fill="#6366f1" />
      <rect x="24" y="54" width="8" height="8" rx="2" fill="#c7d2fe" />
      <path d="M74 56c0-3 2-5 5-5h6c3 0 5 2 5 5v8a10 10 0 0 1-16 8" fill="#fbbf24" opacity=".2" />
      <path d="M72 52h16v10a8 8 0 0 1-16 0V52Z" fill="#fff" stroke="#f59e0b" strokeWidth="2.5" />
      <path d="M88 55h3a4 4 0 0 1 0 8h-3" fill="none" stroke="#f59e0b" strokeWidth="2.5" />
      <path d="M77 46c0-2 2-2 2-4M83 46c0-2 2-2 2-4" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" />
    </>
  ),
  team: (
    <>
      <circle cx="30" cy="34" r="10" fill="#c7d2fe" />
      <path d="M14 66c0-9 7-16 16-16s16 7 16 16" fill="#c7d2fe" />
      <circle cx="70" cy="34" r="10" fill="#c7d2fe" />
      <path d="M54 66c0-9 7-16 16-16s16 7 16 16" fill="#c7d2fe" />
      <circle cx="50" cy="28" r="12" fill="#fff" stroke="#6366f1" strokeWidth="2.5" />
      <path d="M31 70c0-10.5 8.5-19 19-19s19 8.5 19 19" fill="#fff" stroke="#6366f1" strokeWidth="2.5" />
    </>
  ),
  chart: (
    <>
      <rect x="16" y="14" width="68" height="60" rx="6" fill="#fff" stroke="#6366f1" strokeWidth="2.5" />
      <rect x="28" y="46" width="8" height="18" rx="2" fill="#c7d2fe" />
      <rect x="42" y="36" width="8" height="28" rx="2" fill="#6366f1" />
      <rect x="56" y="28" width="8" height="36" rx="2" fill="#c7d2fe" />
      <path d="M28 30c8-6 14 4 20-4s12-4 24-8" stroke="#10b981" strokeWidth="2.5" fill="none" strokeLinecap="round" />
    </>
  ),
  target: (
    <>
      <circle cx="48" cy="46" r="30" fill="#e0e7ff" />
      <circle cx="48" cy="46" r="20" fill="#fff" stroke="#6366f1" strokeWidth="2.5" />
      <circle cx="48" cy="46" r="9" fill="#c7d2fe" stroke="#6366f1" strokeWidth="2.5" />
      <circle cx="48" cy="46" r="2.5" fill="#6366f1" />
      <path d="m48 46 22-22M66 20l8-2-2 8" stroke="#f59e0b" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </>
  ),
  map: (
    <>
      <path d="M18 26 40 18l20 8 22-8v46l-22 8-20-8-22 8V26Z" fill="#fff" stroke="#6366f1" strokeWidth="2.5" strokeLinejoin="round" />
      <path d="M40 18v46M60 26v46" stroke="#c7d2fe" strokeWidth="2.5" />
      <path d="M26 50c8-8 18 6 28-4s14 2 20-6" stroke="#10b981" strokeWidth="2.5" fill="none" strokeLinecap="round" strokeDasharray="5 4" />
      <circle cx="26" cy="50" r="4" fill="#10b981" />
      <circle cx="74" cy="40" r="4" fill="#f59e0b" />
    </>
  ),
  docs: (
    <>
      <rect x="24" y="20" width="46" height="58" rx="6" fill="#e0e7ff" />
      <rect x="32" y="12" width="46" height="58" rx="6" fill="#fff" stroke="#6366f1" strokeWidth="2.5" />
      <path d="M42 28h26M42 38h26M42 48h16" stroke="#c7d2fe" strokeWidth="3" strokeLinecap="round" />
      <circle cx="68" cy="62" r="12" fill="#fff" stroke="#f59e0b" strokeWidth="2.5" />
      <path d="m77 71 7 7" stroke="#f59e0b" strokeWidth="3" strokeLinecap="round" />
    </>
  ),
  chat: (
    <>
      <path d="M16 24a8 8 0 0 1 8-8h36a8 8 0 0 1 8 8v18a8 8 0 0 1-8 8H36l-12 10V50h-.5a8 8 0 0 1-7.5-8V24Z" fill="#fff" stroke="#6366f1" strokeWidth="2.5" strokeLinejoin="round" />
      <path d="M28 27h28M28 36h18" stroke="#c7d2fe" strokeWidth="3" strokeLinecap="round" />
      <path d="M52 56a7 7 0 0 1 7-7h18a7 7 0 0 1 7 7v10a7 7 0 0 1-7 7h-3v8l-10-8h-5a7 7 0 0 1-7-7V56Z" fill="#e0e7ff" />
      <path d="M62 60h14M62 66h9" stroke="#818cf8" strokeWidth="2.5" strokeLinecap="round" />
    </>
  ),
};

/** 双色几何风内联插画（96×88 viewBox）。 */
export function Illustration({
  name,
  className,
}: {
  name: IllustrationName;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 96 88"
      className={className ?? "h-24 w-26"}
      aria-hidden
      fill="none"
    >
      {ILLUSTRATION_PATHS[name]}
    </svg>
  );
}

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

/** 空态占位（可选 illustration：贴合语境的内联双色插画）。 */
export function EmptyState({
  title,
  description,
  illustration,
  children,
}: {
  title: string;
  description?: string;
  /** 内联插画名（如 "notebook"/"calendar"/"team"），不传保持原图标 */
  illustration?: IllustrationName;
  children?: ReactNode;
}) {
  return (
    <div className="edu-fade-up flex flex-col items-center gap-2 rounded-overlay border border-dashed border-line-strong bg-white px-6 py-12 text-center">
      <FxStyles />
      {illustration ? (
        <Illustration name={illustration} className="mb-1 h-22 w-24" />
      ) : (
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
      )}
      <h3 className="text-sm font-semibold text-slate-800">{title}</h3>
      {description && <p className="text-xs text-slate-500">{description}</p>}
      {children}
    </div>
  );
}

/** v3 白底内容容器：中性细边，不用彩色阴影制造“仪表盘卡片”感。 */
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
      className={`rounded-overlay border border-line bg-white ${className ?? ""}`}
    >
      <FxStyles />
      {(title || extra) && (
        <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
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

export function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

export function fmtPct(p: number): string {
  return `${Math.round(p * 100)}%`;
}

/** 友好相对时间（复习到期等）："3 天后" / "2 小时后" / "已到期"。 */
export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffMs = d.getTime() - Date.now();
  const abs = Math.abs(diffMs);
  const MIN = 60_000;
  const HOUR = 60 * MIN;
  const DAY = 24 * HOUR;
  let span: string;
  if (abs < MIN) span = "1 分钟内";
  else if (abs < HOUR) span = `${Math.round(abs / MIN)} 分钟`;
  else if (abs < DAY) span = `${Math.round(abs / HOUR)} 小时`;
  else if (abs < 30 * DAY) span = `${Math.round(abs / DAY)} 天`;
  else span = `${Math.round(abs / (30 * DAY))} 个月`;
  if (abs < MIN) return diffMs >= 0 ? "马上" : "刚刚";
  return diffMs >= 0 ? `${span}后` : `${span}前`;
}

/** 掌握度变化小徽章：老值划线 → 新值（升绿降橙）。 */
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
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
        up
          ? "bg-emerald-50 text-emerald-700 ring-emerald-200/70"
          : "bg-orange-50 text-orange-700 ring-orange-200/70"
      }`}
      title={`知识点 ${change.kc_id}`}
    >
      <span className="max-w-28 truncate">
        {label ?? `KC ${change.kc_id.slice(0, 6)}`}
      </span>
      <span className="font-mono">
        <span className="opacity-55 line-through decoration-current/60">
          {change.old_p.toFixed(2)}
        </span>
        <span aria-hidden>→</span>
        <span className="font-semibold">{change.new_p.toFixed(2)}</span>
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
