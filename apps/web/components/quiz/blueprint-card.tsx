"use client";

// 蓝图确认卡片（F5 interrupt=blueprint_confirm）：items 表格可编辑 类型/难度/知识点，
// 确认/取消 → 由父组件 POST resume {value:{approved, blueprint}}。

import { useState } from "react";

import { Spinner } from "@/components/common";
import type { Blueprint, BlueprintItem } from "@/lib/types";

const TYPE_OPTIONS = [
  { value: "single", label: "单选" },
  { value: "multi", label: "多选" },
  { value: "blank", label: "填空" },
  { value: "short", label: "简答" },
];

const DIFFICULTY_OPTIONS = [
  { value: "easy", label: "简单" },
  { value: "mid", label: "中等" },
  { value: "hard", label: "困难" },
];

export function BlueprintCard({
  blueprint,
  busy,
  onConfirm,
  onCancel,
}: {
  blueprint: Blueprint;
  busy: boolean;
  onConfirm: (edited: Blueprint) => void;
  onCancel: () => void;
}) {
  const [items, setItems] = useState<BlueprintItem[]>(
    blueprint.items.map((it) => ({ ...it })),
  );
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  const patch = (idx: number, patchItem: Partial<BlueprintItem>) => {
    setItems((prev) =>
      prev.map((it, i) => (i === idx ? { ...it, ...patchItem } : it)),
    );
  };

  return (
    <div className="rounded-[14px] border border-brand-100 bg-gradient-to-br from-brand-50/75 to-white p-5">
      <div className="mb-2 flex items-center gap-2">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 text-white">
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
            <path d="M9 11l3 3 8-8M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
          </svg>
        </span>
        <div>
          <h3 className="text-[16px] font-semibold text-ink">练习蓝图</h3>
          <p className="text-xs text-slate-500">
            确认无误后再生成正式题目；每一题都可以单独调整。
          </p>
        </div>
      </div>

      {blueprint.summary && (
        <p className="mb-2 rounded-lg bg-white/70 px-3 py-1.5 text-xs text-slate-600">
          {blueprint.summary}
        </p>
      )}

      <div className="mb-3 grid gap-3 border-y border-brand-100 py-3 text-[11px] sm:grid-cols-4"><div><span className="block text-ink-muted">总题数</span><strong className="mt-1 block text-[14px] text-ink">{items.length} 题</strong></div><div><span className="block text-ink-muted">简单</span><strong className="mt-1 block text-success-600">{items.filter((item) => item.difficulty === "easy").length} 题</strong></div><div><span className="block text-ink-muted">中等</span><strong className="mt-1 block text-brand-700">{items.filter((item) => item.difficulty === "mid").length} 题</strong></div><div><span className="block text-ink-muted">困难</span><strong className="mt-1 block text-warning-600">{items.filter((item) => item.difficulty === "hard").length} 题</strong></div></div>

      <ul className="space-y-2">
        {items.map((it, i) => (
          <li
            key={i}
            className="rounded-[10px] border border-line bg-white p-3"
          >
            <div className="flex items-center gap-3">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[6px] bg-[#efede9] text-[11px] font-semibold text-ink-secondary">{i + 1}</span>
              <div className="min-w-0 flex-1"><p className="truncate text-[12px] font-semibold text-ink">{it.kc_name || "未命名知识点"}</p><p className="mt-0.5 text-[10px] text-ink-muted">{TYPE_OPTIONS.find((option) => option.value === it.type)?.label ?? it.type} · {DIFFICULTY_OPTIONS.find((option) => option.value === it.difficulty)?.label ?? it.difficulty}</p></div>
              <button type="button" onClick={() => setEditingIndex(editingIndex === i ? null : i)} className="rounded-[6px] px-2 py-1 text-[10px] font-medium text-ink-secondary hover:bg-surface-muted hover:text-brand-700">{editingIndex === i ? "完成" : "调整"}</button>
            </div>
            {editingIndex === i && <div className="mt-3 grid gap-2 border-t border-line pt-3 sm:grid-cols-[minmax(0,1fr)_120px_120px]">
              <label className="flex flex-col gap-0.5"><span className="text-[10px] text-ink-muted">知识点</span><input value={it.kc_name} onChange={(e) => patch(i, { kc_name: e.target.value })} className="h-8 rounded-[7px] border border-line-strong px-2 text-[12px] outline-none focus:border-brand-400" /></label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-ink-muted">题型</span>
                <select
                  value={it.type}
                  onChange={(e) => patch(i, { type: e.target.value })}
                  className="h-8 rounded-[7px] border border-line-strong bg-white px-2 text-[12px] outline-none focus:border-brand-400"
                >
                  {TYPE_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-0.5">
                <span className="text-[10px] text-ink-muted">难度</span>
                <select
                  value={it.difficulty}
                  onChange={(e) => patch(i, { difficulty: e.target.value })}
                  className="h-8 rounded-[7px] border border-line-strong bg-white px-2 text-[12px] outline-none focus:border-brand-400"
                >
                  {DIFFICULTY_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>}
          </li>
        ))}
      </ul>

      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          className="rounded-[8px] border border-line-strong bg-white px-4 py-2 text-[12px] font-medium text-ink-secondary hover:bg-surface-muted disabled:cursor-not-allowed disabled:opacity-50"
        >
          取消
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onConfirm({ items, summary: blueprint.summary })}
          className="flex items-center gap-2 rounded-[8px] bg-brand-600 px-4 py-2 text-[12px] font-medium text-white hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}
          确认并生成题目
        </button>
      </div>
    </div>
  );
}
