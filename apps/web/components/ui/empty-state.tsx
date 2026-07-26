// 空状态组件：内置 5 款双色（品牌靛蓝 + 浅灰）内联 SVG 小插画，约 80x80。
// 契约 API（其他模块直接依赖，勿改签名）：
//   import { EmptyState } from "@/components/ui/empty-state";
//   <EmptyState icon="inbox" title="还没有会话" desc="…" action={<button/>} />

import type { ReactNode } from "react";

export type EmptyStateIcon = "inbox" | "search" | "chart" | "book" | "sparkles";

const ICONS: Record<EmptyStateIcon, ReactNode> = {
  inbox: (
    <svg viewBox="0 0 80 80" fill="none" aria-hidden className="h-20 w-20">
      <circle cx="40" cy="40" r="32" fill="#eef2ff" />
      <rect x="24" y="18" width="32" height="28" rx="3" fill="#fff" stroke="#cbd5e1" strokeWidth="2" />
      <path d="M30 26h20M30 32h20M30 38h12" stroke="#e2e8f0" strokeWidth="2.5" strokeLinecap="round" />
      <path
        d="M17 46h13.5l3.5 6h12l3.5-6H63v12a5 5 0 0 1-5 5H22a5 5 0 0 1-5-5Z"
        fill="#6366f1"
      />
      <path
        d="M17 46h13.5l3.5 6h12l3.5-6H63"
        stroke="#4f46e5"
        strokeWidth="2"
        strokeLinejoin="round"
      />
      <path d="M62 16l1.6 3.6 3.6 1.6-3.6 1.6-1.6 3.6-1.6-3.6-3.6-1.6 3.6-1.6Z" fill="#a5b4fc" />
      <circle cx="16" cy="24" r="2.2" fill="#cbd5e1" />
    </svg>
  ),
  search: (
    <svg viewBox="0 0 80 80" fill="none" aria-hidden className="h-20 w-20">
      <circle cx="40" cy="40" r="32" fill="#f1f5f9" />
      <circle cx="36" cy="36" r="15" fill="#fff" stroke="#6366f1" strokeWidth="3" />
      <circle cx="36" cy="36" r="8" fill="#eef2ff" />
      <path d="M30 32.5a7 7 0 0 1 4.5-3.5" stroke="#a5b4fc" strokeWidth="2" strokeLinecap="round" />
      <path d="M47.5 47.5L58 58" stroke="#6366f1" strokeWidth="4.5" strokeLinecap="round" />
      <circle cx="62" cy="26" r="2.5" fill="#cbd5e1" />
      <circle cx="20" cy="55" r="2" fill="#cbd5e1" />
      <circle cx="24" cy="19" r="1.8" fill="#e2e8f0" />
      <path d="M60 44l1.4 3.2 3.2 1.4-3.2 1.4-1.4 3.2-1.4-3.2-3.2-1.4 3.2-1.4Z" fill="#c7d2fe" />
    </svg>
  ),
  chart: (
    <svg viewBox="0 0 80 80" fill="none" aria-hidden className="h-20 w-20">
      <circle cx="40" cy="40" r="32" fill="#eef2ff" />
      <rect x="22" y="42" width="9" height="16" rx="2" fill="#c7d2fe" />
      <rect x="35" y="34" width="9" height="24" rx="2" fill="#a5b4fc" />
      <rect x="48" y="25" width="9" height="33" rx="2" fill="#6366f1" />
      <path
        d="M22 32l11-9 8.5 5L54 16"
        stroke="#94a3b8"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M47 15h8v8" stroke="#94a3b8" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M18 62h44" stroke="#cbd5e1" strokeWidth="2.5" strokeLinecap="round" />
      <circle cx="64" cy="38" r="2" fill="#cbd5e1" />
    </svg>
  ),
  book: (
    <svg viewBox="0 0 80 80" fill="none" aria-hidden className="h-20 w-20">
      <circle cx="40" cy="40" r="32" fill="#f1f5f9" />
      <path
        d="M40 27c-5-4.2-12-5.3-18-4.2v31.5c6-1.1 13 0 18 4.2 5-4.2 12-5.3 18-4.2V22.8c-6-1.1-13 0-18 4.2Z"
        fill="#fff"
        stroke="#6366f1"
        strokeWidth="2.5"
        strokeLinejoin="round"
      />
      <path d="M40 27v31.5" stroke="#c7d2fe" strokeWidth="2.5" />
      <path
        d="M27 31.5c3-.5 6.5-.2 9 .8M27 38.5c3-.5 6.5-.2 9 .8M53 31.5c-3-.5-6.5-.2-9 .8M53 38.5c-3-.5-6.5-.2-9 .8"
        stroke="#e2e8f0"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path d="M58 14l1.8 4 4 1.8-4 1.8-1.8 4-1.8-4-4-1.8 4-1.8Z" fill="#a5b4fc" />
      <circle cx="21" cy="17" r="2" fill="#cbd5e1" />
    </svg>
  ),
  sparkles: (
    <svg viewBox="0 0 80 80" fill="none" aria-hidden className="h-20 w-20">
      <circle cx="40" cy="40" r="32" fill="#eef2ff" />
      <path
        d="M38 16l4.7 12.6L55.3 33.3 42.7 38 38 50.6 33.3 38 20.7 33.3l12.6-4.7Z"
        fill="#6366f1"
      />
      <path d="M56 42l2.7 6.6 6.6 2.7-6.6 2.7L56 60.6l-2.7-6.6-6.6-2.7 6.6-2.7Z" fill="#a5b4fc" />
      <path d="M26 48l1.9 4.4 4.4 1.9-4.4 1.9L26 60.6l-1.9-4.4-4.4-1.9 4.4-1.9Z" fill="#cbd5e1" />
      <path d="M18 26a26 26 0 0 1 7-7" stroke="#cbd5e1" strokeWidth="2" strokeLinecap="round" />
      <circle cx="62" cy="22" r="2.2" fill="#cbd5e1" />
    </svg>
  ),
};

export function EmptyState({
  icon = "inbox",
  title,
  desc,
  action,
  className = "",
}: {
  icon?: EmptyStateIcon;
  title: string;
  desc?: string;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex animate-fade-up flex-col items-center justify-center px-6 py-12 text-center ${className}`.trim()}
    >
      <div className="mb-3">{ICONS[icon]}</div>
      <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
      {desc && <p className="mt-1 max-w-xs text-sm text-slate-500">{desc}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
