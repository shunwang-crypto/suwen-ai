"use client";

import { useState } from "react";

import { ModelMarkdown } from "@/components/chat/model-markdown";
import { ApiError, apiJson } from "@/lib/api";
import type { ExplainBackResult } from "@/lib/types";

export function ExplainBackPanel({
  kcId,
  promptSpanRef,
}: {
  kcId?: string;
  promptSpanRef: string;
}) {
  const [studentText, setStudentText] = useState("");
  const [result, setResult] = useState<ExplainBackResult | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const normalized = studentText.trim();
    if (!normalized || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const next = await apiJson<ExplainBackResult>("/api/metacognition/explain-back", {
        body: {
          student_text: normalized,
          prompt_span_ref: promptSpanRef,
          ...(kcId ? { kc_id: kcId } : {}),
        },
      });
      setResult(next);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "表达反馈暂时不可用，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <details className="mt-3 border-t border-black/5 pt-3">
      <summary className="cursor-pointer text-body-sm font-medium text-brand-700">
        用自己的话反讲一次
      </summary>
      <div className="mt-2 rounded-[9px] bg-white/75 p-3">
        <label className="block text-body-sm font-medium text-ink-secondary">
          不看解析，写下你对关键概念或解题步骤的解释
          <textarea
            value={studentText}
            onChange={(event) => setStudentText(event.target.value)}
            maxLength={3000}
            rows={3}
            disabled={submitting}
            className="mt-1.5 w-full resize-y rounded-[8px] border border-line-strong bg-white px-3 py-2 text-body-sm leading-5 text-ink outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-100 disabled:opacity-60"
            placeholder="例如：为什么这里可以使用这个公式？关键条件是什么？"
          />
        </label>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <p className="text-caption text-ink-muted">
            AI 只反馈表达完整度，不代表掌握度，也不会写入学习画像。
          </p>
          <button
            type="button"
            disabled={!studentText.trim() || submitting}
            onClick={() => void submit()}
            className="rounded-[7px] bg-brand-600 px-3 py-1.5 text-body-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? "分析中…" : result ? "重新获取反馈" : "获取表达反馈"}
          </button>
        </div>
        {error && <p role="alert" className="mt-2 text-body-sm text-danger-600">{error}</p>}
        {result && (
          <div
            className="mt-3 space-y-2 rounded-[8px] border border-line bg-[#fafaf9] p-3 text-body-sm leading-5 text-ink-secondary"
            aria-live="polite"
          >
            <div className="flex flex-wrap items-center gap-2">
              <strong className="text-ink">表达反馈</strong>
              {result.understanding_score != null && (
                <span className="rounded-full bg-brand-50 px-2 py-0.5 text-caption font-medium text-brand-700">
                  完整度 {Math.round(result.understanding_score * 100)}%
                </span>
              )}
              {result.degraded && (
                <span className="rounded-full bg-warning-50 px-2 py-0.5 text-caption text-warning-700">
                  自动分析暂不可用
                </span>
              )}
            </div>
            <div><ModelMarkdown text={result.feedback} /></div>
            {result.gap && <div><p className="font-medium text-ink">可补充：</p><ModelMarkdown text={result.gap} /></div>}
            {result.retry_guidance && <div><p className="font-medium text-ink">下一步：</p><ModelMarkdown text={result.retry_guidance} /></div>}
          </div>
        )}
      </div>
    </details>
  );
}
