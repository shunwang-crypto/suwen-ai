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

  const patch = (idx: number, patchItem: Partial<BlueprintItem>) => {
    setItems((prev) =>
      prev.map((it, i) => (i === idx ? { ...it, ...patchItem } : it)),
    );
  };

  return (
    <div className="rounded-2xl border border-indigo-200 bg-indigo-50/50 p-4">
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
          <h3 className="text-sm font-semibold text-slate-900">出题蓝图待确认</h3>
          <p className="text-xs text-slate-500">
            工作流已暂停。可直接调整每题的知识点/类型/难度，确认后才会真正生成题目。
          </p>
        </div>
      </div>

      {blueprint.summary && (
        <p className="mb-2 rounded-lg bg-white/70 px-3 py-1.5 text-xs text-slate-600">
          {blueprint.summary}
        </p>
      )}

      <div className="overflow-x-auto rounded-xl border border-indigo-100 bg-white">
        <table className="w-full min-w-105 text-left text-sm">
          <thead>
            <tr className="border-b border-slate-100 text-xs text-slate-400">
              <th className="px-3 py-2 font-medium">#</th>
              <th className="px-3 py-2 font-medium">知识点</th>
              <th className="px-3 py-2 font-medium">题型</th>
              <th className="px-3 py-2 font-medium">难度</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it, i) => (
              <tr key={i} className="border-b border-slate-50">
                <td className="px-3 py-1.5 text-xs text-slate-400">{i + 1}</td>
                <td className="px-3 py-1.5">
                  <input
                    value={it.kc_name}
                    onChange={(e) => patch(i, { kc_name: e.target.value })}
                    className="w-full min-w-32 rounded-lg border border-slate-200 px-2 py-1 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
                  />
                </td>
                <td className="px-3 py-1.5">
                  <select
                    value={it.type}
                    onChange={(e) => patch(i, { type: e.target.value })}
                    className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-sm outline-none focus:border-indigo-500"
                  >
                    {TYPE_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="px-3 py-1.5">
                  <select
                    value={it.difficulty}
                    onChange={(e) => patch(i, { difficulty: e.target.value })}
                    className="rounded-lg border border-slate-200 bg-white px-2 py-1 text-sm outline-none focus:border-indigo-500"
                  >
                    {DIFFICULTY_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          className="rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          取消
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onConfirm({ items, summary: blueprint.summary })}
          className="flex items-center gap-2 rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}
          确认并生成题目
        </button>
      </div>
    </div>
  );
}
