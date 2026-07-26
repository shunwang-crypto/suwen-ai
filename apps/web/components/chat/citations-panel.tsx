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
}: {
  citations: CitationData[];
  activeIdx: number | null;
  onSelect: (idx: number) => void;
  onOpenContext?: (citation: CitationData) => void;
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
        return (
          <button
            key={`${c.chunk_id}-${c.idx}`}
            type="button"
            data-citation-idx={c.idx}
            onClick={() => {
              onSelect(c.idx);
              onOpenContext?.(c);
            }}
            className={`block w-full rounded-xl border p-3 text-left transition-colors ${
              active
                ? "border-indigo-400 bg-indigo-50/60 ring-2 ring-indigo-100"
                : "border-slate-200 bg-white hover:border-indigo-200 hover:bg-indigo-50/30"
            }`}
          >
            <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
              <span
                className={`flex h-5 min-w-5 items-center justify-center rounded px-1 text-xs font-semibold ${
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
                  className="rounded-full bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
                  title={`来源知识库：${c.kb_name}`}
                >
                  {c.kb_name}
                </span>
              )}
              {c.sanitized && (
                <span
                  className="rounded border border-red-200 bg-red-50 px-1.5 py-0.5 text-xs font-medium text-red-600"
                  title="该引用块中检测到疑似提示词注入内容，已被过滤"
                >
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
