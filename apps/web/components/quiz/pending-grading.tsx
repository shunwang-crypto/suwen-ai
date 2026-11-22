"use client";

export function PendingGrading({ count = 1, retryAvailable, busy, onRetry }: {
  count?: number;
  retryAvailable: boolean;
  busy: boolean;
  onRetry: () => void;
}) {
  return (
    <div role="status" className="mt-4 rounded-control border border-line-strong bg-surface-muted px-4 py-3 text-body-sm text-ink-secondary">
      <p className="font-semibold text-ink">{count} 题待评分</p>
      <p className="mt-1">作答已保存；这些题暂不计入正确率，不更新掌握度、错题或复习进度。</p>
      {retryAvailable ? (
        <button type="button" disabled={busy} onClick={onRetry} className="mt-3 rounded-control border border-line-strong bg-white px-3 py-1.5 font-medium text-ink disabled:opacity-50">
          {busy ? "重新评分中…" : "重新评分已保存的答案"}
        </button>
      ) : <p className="mt-2">自动评分暂未成功，已保存答案供后续复核。</p>}
    </div>
  );
}
