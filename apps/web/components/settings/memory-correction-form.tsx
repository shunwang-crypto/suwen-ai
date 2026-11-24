"use client";

import { useState } from "react";

interface Props {
  id: string;
  text: string;
  kind: string;
  busy: boolean;
  onSave: (patch: { text: string; kind: string }) => void;
  onCancel: () => void;
}

export function MemoryCorrectionForm({ id, text, kind, busy, onSave, onCancel }: Props) {
  const [draftText, setDraftText] = useState(text);
  const [draftKind, setDraftKind] = useState(kind);
  return (
    <form className="mt-3 space-y-2" onSubmit={(event) => {
      event.preventDefault();
      if (!busy && draftText.trim()) onSave({ text: draftText.trim(), kind: draftKind });
    }}>
      <label className="block text-xs text-ink-secondary" htmlFor={`memory-text-${id}`}>更正内容</label>
      <textarea id={`memory-text-${id}`} value={draftText} onChange={(event) => setDraftText(event.target.value)}
        disabled={busy} maxLength={600} required rows={3}
        className="w-full rounded-control border border-line-strong bg-surface px-3 py-2 text-sm text-ink" />
      <label className="flex items-center gap-2 text-xs text-ink-secondary">现在的情况
        <select value={draftKind} onChange={(event) => setDraftKind(event.target.value)} disabled={busy}
          className="rounded-control border border-line-strong bg-surface px-2 py-1.5 text-xs">
          <option value="confused">仍有困惑</option>
          <option value="understood">已经理解</option>
          <option value="misconception">需要纠正的理解</option>
        </select>
      </label>
      <div className="flex gap-3">
        <button type="submit" disabled={busy || !draftText.trim()}
          className="rounded-control bg-ink px-3 py-1.5 text-xs text-white disabled:opacity-50">保存纠正</button>
        <button type="button" disabled={busy} onClick={onCancel} className="text-xs text-ink-secondary">取消</button>
      </div>
    </form>
  );
}
