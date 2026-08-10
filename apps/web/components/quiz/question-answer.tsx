"use client";

// 作答控件（F5/F6 共用）：单选/多选/填空/简答四种题型，题干与选项过 <MathText>。
// multi 的作答值序列化为 "A,C" 式字母组合，与后端 grade_objective 对齐。

import { MathText } from "@/lib/katex";
import type { QuizQuestion } from "@/lib/types";

const LETTERS = ["A", "B", "C", "D", "E", "F"];

export const TYPE_LABELS: Record<string, string> = {
  single: "单选",
  multi: "多选",
  blank: "填空",
  short: "简答",
};

export const DIFFICULTY_LABELS: Record<string, string> = {
  easy: "简单",
  mid: "中等",
  hard: "困难",
};

function toLetterSet(v: string): Set<string> {
  return new Set(
    v
      .split(",")
      .map((t) => t.trim().toUpperCase())
      .filter(Boolean),
  );
}

export function QuestionAnswer({
  question,
  value,
  disabled,
  onChange,
}: {
  question: QuizQuestion;
  value: string;
  disabled?: boolean;
  onChange: (v: string) => void;
}) {
  const { type, options } = question;

  if (type === "single") {
    return (
      <div className="space-y-1.5">
        {options.map((opt, i) => {
          const letter = LETTERS[i] ?? String(i + 1);
          const checked = value.trim().toUpperCase() === letter;
          return (
            <label
              key={i}
              className={`flex cursor-pointer items-start gap-2.5 rounded-[9px] border px-3 py-2.5 text-body-sm transition-colors ${
                checked
                  ? "border-brand-400 bg-brand-50 text-brand-900 ring-1 ring-inset ring-brand-100"
                  : "border-line bg-white text-ink-secondary hover:border-brand-200 hover:bg-brand-50/30"
              } ${disabled ? "cursor-default opacity-70" : ""}`}
            >
              <input
                type="radio"
                name={`q-${question.id}`}
                checked={checked}
                disabled={disabled}
                onChange={() => onChange(letter)}
                className="mt-1 accent-indigo-600"
              />
              <span className="min-w-0">
                <span className="mr-1.5 font-semibold">{letter}.</span>
                <MathText text={opt} />
              </span>
            </label>
          );
        })}
      </div>
    );
  }

  if (type === "multi") {
    const selected = toLetterSet(value);
    const toggle = (letter: string) => {
      const next = new Set(selected);
      if (next.has(letter)) next.delete(letter);
      else next.add(letter);
      onChange([...next].sort().join(","));
    };
    return (
      <div className="space-y-1.5">
        {options.map((opt, i) => {
          const letter = LETTERS[i] ?? String(i + 1);
          const checked = selected.has(letter);
          return (
            <label
              key={i}
              className={`flex cursor-pointer items-start gap-2.5 rounded-[9px] border px-3 py-2.5 text-body-sm transition-colors ${
                checked
                  ? "border-brand-400 bg-brand-50 text-brand-900 ring-1 ring-inset ring-brand-100"
                  : "border-line bg-white text-ink-secondary hover:border-brand-200 hover:bg-brand-50/30"
              } ${disabled ? "cursor-default opacity-70" : ""}`}
            >
              <input
                type="checkbox"
                checked={checked}
                disabled={disabled}
                onChange={() => toggle(letter)}
                className="mt-1 accent-indigo-600"
              />
              <span className="min-w-0">
                <span className="mr-1.5 font-semibold">{letter}.</span>
                <MathText text={opt} />
              </span>
            </label>
          );
        })}
      </div>
    );
  }

  if (type === "blank") {
    return (
      <input
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        placeholder="填写答案"
        className="w-full rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-50"
      />
    );
  }

  // short：简答
  return (
    <textarea
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      rows={3}
      maxLength={8000}
      placeholder="写下你的解答（简答题由 AI 对照参考答案评分）"
      className="w-full resize-y rounded-xl border border-slate-300 px-3.5 py-2 text-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-50"
    />
  );
}

/** 题头徽章：题型 + 难度。 */
export function QuestionMeta({ question }: { question: QuizQuestion }) {
  return (
    <span className="flex shrink-0 items-center gap-1.5">
      <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-body-sm font-medium text-indigo-600">
        {TYPE_LABELS[question.type] ?? question.type}
      </span>
      <span
        className={`rounded-full px-2 py-0.5 text-body-sm font-medium ${
          question.difficulty === "hard"
            ? "bg-red-50 text-red-600"
            : question.difficulty === "easy"
              ? "bg-emerald-50 text-emerald-600"
              : "bg-amber-50 text-amber-600"
        }`}
      >
        {DIFFICULTY_LABELS[question.difficulty] ?? question.difficulty}
      </span>
    </span>
  );
}
