"use client";

import { useEffect, useId, useRef } from "react";

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
  const dialogRef = useRef<HTMLElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);
  const titleId = useId();

  useEffect(() => {
    if (open && !wasOpenRef.current) {
      restoreFocusRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      wasOpenRef.current = true;
      cancelRef.current?.focus({ preventScroll: true });
    } else if (!open && wasOpenRef.current) {
      wasOpenRef.current = false;
      restoreFocusRef.current?.focus({ preventScroll: true });
      restoreFocusRef.current = null;
    }
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (!busy) onCancel();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex=\"-1\"])",
        ),
      );
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [busy, onCancel, open]);

  useEffect(() => () => {
    if (wasOpenRef.current) {
      restoreFocusRef.current?.focus({ preventScroll: true });
    }
  }, []);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center px-4">
      <button type="button" aria-label="取消确认" tabIndex={-1} disabled={busy} className="absolute inset-0 bg-black/40" onClick={onCancel} />
      <section ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby={titleId} className="relative w-full max-w-[440px] rounded-overlay border border-line bg-white p-5 shadow-floating">
        <h2 id={titleId} className="text-[17px] font-semibold text-ink">{title}</h2>
        <p className="mt-3 text-body-sm leading-5 text-ink-secondary">{description}</p>
        <div className="mt-6 flex justify-end gap-2">
          <button ref={cancelRef} type="button" disabled={busy} onClick={onCancel} className="rounded-control border border-line-strong px-4 py-2 text-body-sm font-medium text-ink-secondary disabled:opacity-40">{cancelLabel}</button>
          <button type="button" disabled={busy} onClick={onConfirm} className={`rounded-control px-4 py-2 text-body-sm font-medium text-white disabled:opacity-40 ${destructive ? "bg-danger-600 hover:bg-red-700" : "bg-brand-600 hover:bg-brand-800"}`}>{busy ? "处理中…" : confirmLabel}</button>
        </div>
      </section>
    </div>
  );
}
