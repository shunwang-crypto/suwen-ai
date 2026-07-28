"use client";

// 练习发起表单（F5）：主题 / 绑定知识库 / 难度配比（题数 = 三档之和，1-20）。

import { useState, type FormEvent } from "react";

import { Spinner } from "@/components/common";
import type { KbOut } from "@/lib/types";

export interface QuizFormValue {
  topic: string;
  kb_id?: string;
  num_questions: number;
  difficulty_mix: { easy: number; mid: number; hard: number };
}

function MixInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-slate-500">{label}</span>
      <input
        type="number"
        min={0}
        max={20}
        value={value}
        onChange={(e) =>
          onChange(Math.max(0, Math.min(20, Math.trunc(Number(e.target.value) || 0))))
        }
        className="w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
      />
    </label>
  );
}

export function QuizForm({
  kbs,
  busy,
  onSubmit,
  initialTopic,
  initialKbId,
}: {
  kbs: KbOut[];
  busy: boolean;
  onSubmit: (value: QuizFormValue) => void;
  /** URL 预填（/quiz?topic=..&kb=..，对话页「考考我」chip 跳转）；变化时由调用方换 key 重挂载 */
  initialTopic?: string;
  initialKbId?: string;
}) {
  const [topic, setTopic] = useState(initialTopic ?? "");
  const [kbId, setKbId] = useState(initialKbId ?? "");
  const [easy, setEasy] = useState(2);
  const [mid, setMid] = useState(2);
  const [hard, setHard] = useState(1);

  const total = easy + mid + hard;
  const valid = topic.trim().length > 0 && total >= 1 && total <= 20;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid || busy) return;
    onSubmit({
      topic: topic.trim(),
      kb_id: kbId || undefined,
      num_questions: total,
      difficulty_mix: { easy, mid, hard },
    });
  };

  return (
    <form onSubmit={submit} className="space-y-3">
      <label className="block">
        <span className="mb-1 block text-xs font-medium text-slate-500">练习主题</span>
        <input
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          maxLength={255}
          placeholder="如：泰勒展开、二次函数图像性质"
          className="w-full rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
        />
      </label>

      <label className="block">
        <span className="mb-1 block text-xs font-medium text-slate-500">
          绑定知识库（可选，出题将取材于库内文档）
        </span>
        <select
          value={kbId}
          onChange={(e) => setKbId(e.target.value)}
          className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
        >
          <option value="">不绑定（通用出题）</option>
          {kbs.map((kb) => (
            <option key={kb.id} value={kb.id}>
              {kb.name}
            </option>
          ))}
        </select>
      </label>

      <div>
        <span className="mb-1 block text-xs font-medium text-slate-500">
          难度配比（题数 = 三档之和，共 {total} 题，上限 20）
        </span>
        <div className="grid grid-cols-3 gap-2">
          <MixInput label="简单" value={easy} onChange={setEasy} />
          <MixInput label="中等" value={mid} onChange={setMid} />
          <MixInput label="困难" value={hard} onChange={setHard} />
        </div>
        {total > 20 && (
          <p className="mt-1 text-xs text-red-600">总题数不能超过 20</p>
        )}
        {total === 0 && (
          <p className="mt-1 text-xs text-slate-400">至少需要 1 题</p>
        )}
      </div>

      <button
        type="submit"
        disabled={!valid || busy}
        className="flex w-full items-center justify-center gap-2 rounded-control bg-ink py-2.5 text-sm font-semibold text-white transition-colors hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
      >
        {busy && <Spinner className="h-4 w-4 border-white/40 border-t-white" />}
        {busy ? "正在生成蓝图…" : "开始出题"}
      </button>
    </form>
  );
}
