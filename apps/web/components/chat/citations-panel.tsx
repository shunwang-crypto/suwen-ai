"use client";

// 引用面板：citation 事件卡片；点击卡片与正文 [N] 双向联动高亮，
// 并打开「引用回原文」抽屉（onOpenContext，由 chat 页装配 ContextDrawer）。

import { useEffect, useRef } from "react";

import { MathText } from "@/lib/katex";

import type { CitationData } from "@/lib/types";

export function CitationsPanel({
  citations,
  activeIdx,
  onSelect,
  onOpenContext,
  kbOwnerTypes,
}: {
  citations: CitationData[];
  activeIdx: number | null;
  onSelect: (idx: number) => void;
  onOpenContext?: (citation: CitationData) => void;
  /** kb_id → 归属类型（个人/机构），仅用于徽章与色条配色 */
  kbOwnerTypes?: Record<string, "user" | "org">;
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  // 正文点击 [N] 时滚动到对应卡片
  useEffect(() => {
    if (activeIdx === null || !containerRef.current) return;
    const el = containerRef.current.querySelector(
      `[data-citation-idx="${activeIdx}"]`,
    );
    el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [activeIdx]);

  if (citations.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center">
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-8 w-8 text-slate-300"
          aria-hidden
        >
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M9 13h6M9 17h6" />
        </svg>
        <p className="text-sm text-slate-400">
          暂无引用。绑定知识库提问后，
          <br />
          回答的依据会展示在这里。
        </p>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="h-full space-y-2.5 overflow-y-auto p-3">
      {citations.map((c) => {
        const active = activeIdx === c.idx;
        // 来源归属：优先 chat 页传入的 kb 映射，回退引用自带字段（可选扩展字段）
        const ownerType =
          (c.kb_id ? kbOwnerTypes?.[c.kb_id] : undefined) ??
          (c.kb_owner_type === "org" || c.kb_owner_type === "user"
            ? c.kb_owner_type
            : undefined);
        // 左侧来源色条：机构库琥珀 / 个人库天蓝 / 未知归属靛蓝
        const stripClass =
          ownerType === "org"
            ? "from-amber-500 to-orange-400"
            : ownerType === "user"
              ? "from-sky-500 to-cyan-400"
              : "from-indigo-500 to-violet-400";
        return (
          <button
            key={`${c.chunk_id}-${c.idx}`}
            type="button"
            data-citation-idx={c.idx}
            onClick={() => {
              onSelect(c.idx);
              onOpenContext?.(c);
            }}
            className={`fx-msg-in relative block w-full overflow-hidden rounded-xl border p-3 pl-4 text-left transition-all duration-200 ${
              active
                ? "-translate-y-0.5 border-indigo-400 bg-indigo-50/70 shadow-md shadow-indigo-600/10 ring-2 ring-indigo-200"
                : "border-slate-200 bg-white hover:-translate-y-0.5 hover:border-indigo-200 hover:bg-indigo-50/30 hover:shadow-md hover:shadow-indigo-600/5"
            }`}
          >
            <span
              aria-hidden
              className={`absolute inset-y-0 left-0 w-1 bg-gradient-to-b ${stripClass} ${
                active ? "opacity-100" : "opacity-70"
              }`}
            />
            <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
              <span
                className={`flex h-5 min-w-5 items-center justify-center rounded px-1 text-xs font-semibold transition-colors ${
                  active
                    ? "bg-indigo-600 text-white"
                    : "bg-indigo-100 text-indigo-700"
                }`}
              >
                {c.idx}
              </span>
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-800">
                {c.doc_name || "未知文档"}
              </span>
              {c.kb_name && (
                <span
                  className={`inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${
                    ownerType === "org"
                      ? "border-amber-200 bg-amber-50 text-amber-700"
                      : "border-sky-200 bg-sky-50 text-sky-700"
                  }`}
                  title={`来源知识库：${c.kb_name}（${ownerType === "org" ? "机构库" : "个人库"}）`}
                >
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-2.5 w-2.5 shrink-0"
                    aria-hidden
                  >
                    {ownerType === "org" ? (
                      <path d="M3 21h18M5 21V7l7-4 7 4v14M9 9h.01M9 13h.01M15 9h.01M15 13h.01" />
                    ) : (
                      <path d="M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 10a7 7 0 0 1 14 0" />
                    )}
                  </svg>
                  {c.kb_name}
                </span>
              )}
              {c.sanitized && (
                <span
                  className="inline-flex items-center gap-1 rounded-full border border-rose-200 bg-rose-50 px-1.5 py-0.5 text-[10px] font-medium text-rose-600"
                  title="该引用块中检测到疑似提示词注入内容，已被过滤"
                >
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-2.5 w-2.5 shrink-0"
                    aria-hidden
                  >
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Zm-3-13 6 6m0-6-6 6" />
                  </svg>
                  已过滤注入内容
                </span>
              )}
            </div>
            {(c.heading_path || c.page !== null) && (
              <p className="mb-1 truncate text-xs text-slate-400">
                {c.heading_path}
                {c.heading_path && c.page != null && " · "}
                {c.page != null && `第 ${c.page} 页`}
              </p>
            )}
            {c.highlight && (
              <p className="line-clamp-4 border-l-2 border-indigo-200 pl-2 text-xs leading-relaxed text-slate-600">
                <MathText text={c.highlight} />
              </p>
            )}
          </button>
        );
      })}
    </div>
  );
}
