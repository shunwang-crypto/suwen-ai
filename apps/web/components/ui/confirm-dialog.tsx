"use client";

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "确认",
  cancelLabel = "取消",
  destructive = false,
  busy = false,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center px-4">
      <button type="button" aria-label="取消确认" className="absolute inset-0 bg-black/40" onClick={busy ? undefined : onCancel} />
      <section role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title" className="relative w-full max-w-[440px] rounded-[14px] border border-line bg-white p-5 shadow-floating">
        <h2 id="confirm-dialog-title" className="text-[17px] font-semibold text-ink">{title}</h2>
        <p className="mt-3 text-[12px] leading-5 text-ink-secondary">{description}</p>
        <div className="mt-6 flex justify-end gap-2">
          <button type="button" disabled={busy} onClick={onCancel} className="rounded-[8px] border border-line-strong px-4 py-2 text-[12px] font-medium text-ink-secondary disabled:opacity-40">{cancelLabel}</button>
          <button type="button" disabled={busy} onClick={onConfirm} className={`rounded-[8px] px-4 py-2 text-[12px] font-medium text-white disabled:opacity-40 ${destructive ? "bg-danger-600 hover:bg-red-700" : "bg-brand-600 hover:bg-brand-800"}`}>{busy ? "处理中…" : confirmLabel}</button>
        </div>
      </section>
    </div>
  );
}
