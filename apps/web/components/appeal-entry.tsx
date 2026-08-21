"use client";

// 判分申诉入口（P2-3）：主观题（LLM 评分）结果下方的「申诉复核」按钮 + 理由表单 +
// 结果反馈。按 question_id 提交，后端定位本人最近一次 LLM 判分的作答复核；
// 复核分差 ≥0.3 修正并同步掌握度，<0.3 维持原判，stub 网关下 202 登记待人工。
// quiz 结果页与复习/错题本的主观题行共用本组件。

import { useState } from "react";

import { MasteryDelta, Spinner } from "@/components/common";
import { ApiError, apiJson } from "@/lib/api";
import type { AppealResult } from "@/lib/types";

export function AppealEntry({ questionId }: { questionId: string }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AppealResult | null>(null);

  const submit = async () => {
    if (busy || !reason.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const res = await apiJson<AppealResult>("/api/appeals", {
        body: { question_id: questionId, reason: reason.trim() },
      });
      setResult(res);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.status === 404
            ? "该题暂无可申诉的 AI 评分记录"
            : err.code === "APPEAL_NOT_ALLOWED"
              ? "客观题为规则判分，无需申诉"
              : err.message,
        );
      } else {
        setError("申诉提交失败，请重试");
      }
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    const tone =
      result.status === "adjusted"
        ? "border-emerald-200 bg-emerald-50/70 text-emerald-800"
        : result.status === "pending_manual_review"
          ? "border-amber-200 bg-amber-50/70 text-amber-800"
          : "border-line bg-surface-muted text-ink-secondary";
    return (
      <div className={`mt-2 rounded-xl border px-3 py-2 text-xs ${tone}`}>
        <p className="font-medium">
          {result.status === "adjusted"
            ? "申诉复核已修正"
            : result.status === "pending_manual_review"
              ? "申诉已登记"
              : "申诉复核完成"}
        </p>
        <p className="mt-0.5">{result.message}</p>
        {result.old_score != null && result.new_score != null && (
          <p className="mt-0.5 font-mono">
            {(result.old_score * 100).toFixed(0)} 分 →{" "}
            {(result.new_score * 100).toFixed(0)} 分
          </p>
        )}
        {result.mastery_changes.length > 0 && (
          <p className="mt-1.5 flex flex-wrap gap-1.5">
            {result.mastery_changes.map((c) => (
              <MasteryDelta key={c.kc_id} change={c} />
            ))}
          </p>
        )}
      </div>
    );
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-2 text-xs font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
      >
        对 AI 评分有异议？申诉复核
      </button>
    );
  }

  return (
    <div className="mt-2 space-y-1.5">
      <textarea
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        rows={2}
        maxLength={2000}
        placeholder="说明你认为评分有误的理由（如：参考答案的哪一点我其实答到了）…"
        className="w-full resize-none rounded-xl border border-line-strong px-3 py-2 text-xs outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
      />
      {error && <p className="text-xs text-red-600">{error}</p>}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => setOpen(false)}
          className="rounded-lg border border-line-strong px-3 py-1 text-xs text-ink-muted hover:bg-surface-muted disabled:opacity-50"
        >
          取消
        </button>
        <button
          type="button"
          disabled={busy || !reason.trim()}
          onClick={() => void submit()}
          className="flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1 text-xs font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy && <Spinner className="h-3 w-3 border-white/40 border-t-white" />}
          {busy ? "复核中…" : "提交申诉"}
        </button>
      </div>
    </div>
  );
}
