"use client";

import { useRef, useState, type FormEvent } from "react";

import { ApiError, apiFetch, apiJson } from "@/lib/api";
import type { LearningResourceOut, LearningResourceType } from "@/lib/types";

const RESOURCE_TYPES: Array<[LearningResourceType, string]> = [
  ["exam", "真实试卷"],
  ["mock_exam", "模拟卷"],
  ["workbook", "练习册"],
  ["homework", "作业"],
  ["question_set", "题单"],
  ["textbook", "教材"],
  ["lecture", "讲义"],
  ["notes", "笔记"],
  ["other", "其他资料"],
];

const ACCEPT = ".png,.jpg,.jpeg,.webp,.pdf,.docx,.pptx,.txt,.md";
const fieldClass = "mt-1 h-9 w-full rounded-[8px] border border-line-strong bg-white px-3 text-[12px] text-ink outline-none transition-colors focus:border-brand-500";

function friendlyError(reason: unknown): string {
  if (reason instanceof ApiError) {
    const messages: Record<string, string> = {
      RESOURCE_QUOTA_EXCEEDED: "学习资料数量已达上限。",
      TOO_MANY_PENDING_RESOURCES: "待识别资料过多，请等已有资料处理完成。",
      STORAGE_QUOTA_EXCEEDED: "个人存储空间已达上限。",
      RESOURCE_FILE_TOO_LARGE: "文件超过单文件大小限制。",
      FILE_SIGNATURE_MISMATCH: "文件内容与扩展名不匹配，无法上传。",
      UNSUPPORTED_FILE_TYPE: "暂不支持这个文件类型。",
      SOURCE_SAVE_FAILED: "原文件保存失败，请重试。",
    };
    return messages[reason.code] ?? reason.message;
  }
  return reason instanceof Error ? reason.message : "创建学习资料失败，请稍后重试";
}

export function ResourceUploadDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (resource: LearningResourceOut) => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState("");
  const [subject, setSubject] = useState("");
  const [resourceType, setResourceType] = useState<LearningResourceType>("exam");
  const [file, setFile] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (saving) return;
    if (!title.trim() && !file) {
      setError("请填写资料标题，或选择一个文件");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      let resource: LearningResourceOut;
      if (file) {
        const form = new FormData();
        form.append("file", file);
        form.append("title", title.trim() || file.name);
        form.append("subject", subject.trim() || "general");
        form.append("resource_type", resourceType);
        const response = await apiFetch("/api/resources", { method: "POST", body: form });
        resource = (await response.json()) as LearningResourceOut;
      } else {
        resource = await apiJson<LearningResourceOut>("/api/resources", {
          body: {
            title: title.trim(),
            subject: subject.trim() || "general",
            resource_type: resourceType,
          },
        });
      }
      onCreated(resource);
    } catch (reason) {
      setError(friendlyError(reason));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-4">
      <button type="button" aria-label="关闭创建学习资料" className="absolute inset-0 bg-[#1c1917]/40" onClick={saving ? undefined : onClose} />
      <section role="dialog" aria-modal="true" aria-labelledby="resource-upload-title" className="relative w-full max-w-[560px] overflow-hidden rounded-[14px] border border-line bg-canvas shadow-floating">
        <header className="flex items-start justify-between border-b border-line bg-white px-5 py-4">
          <div>
            <p className="text-[10px] font-medium uppercase tracking-[0.1em] text-ink-muted">学习资料</p>
            <h2 id="resource-upload-title" className="mt-1 text-[18px] font-semibold tracking-[-0.02em] text-ink">导入试卷或创建资料</h2>
            <p className="mt-1 text-[11px] text-ink-secondary">题目资料只进入校对和真实作答记录，不会自动进入普通知识库。</p>
          </div>
          <button type="button" onClick={onClose} disabled={saving} aria-label="关闭" className="rounded-[7px] p-2 text-ink-muted hover:bg-surface-muted disabled:opacity-40">✕</button>
        </header>
        <form onSubmit={submit} className="space-y-4 p-5">
          {error && <p className="rounded-[8px] border border-danger-200 bg-danger-50 px-3 py-2 text-[11px] text-danger-700">{error}</p>}
          <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_160px]">
            <label className="block">
              <span className="text-[10px] font-medium text-ink-secondary">标题</span>
              <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={255} placeholder="例如：高一数学期中模拟卷" className={fieldClass} />
            </label>
            <label className="block">
              <span className="text-[10px] font-medium text-ink-secondary">资料类型</span>
              <select value={resourceType} onChange={(event) => setResourceType(event.target.value as LearningResourceType)} className={fieldClass}>
                {RESOURCE_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
          </div>
          <label className="block">
            <span className="text-[10px] font-medium text-ink-secondary">科目</span>
            <input value={subject} onChange={(event) => setSubject(event.target.value)} maxLength={64} placeholder="例如：数学" className={fieldClass} />
          </label>
          <div className="rounded-[10px] border border-dashed border-line-strong bg-white p-4">
            <input
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              className="sr-only"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-[12px] font-medium text-ink">{file?.name ?? "选择原始文件（可选）"}</p>
                <p className="mt-1 text-[10px] leading-4 text-ink-muted">支持图片、PDF、Word、PPT、TXT 和 Markdown。没有文件也可以手动添加题目。</p>
              </div>
              <button type="button" onClick={() => fileRef.current?.click()} className="shrink-0 rounded-[8px] border border-line-strong px-3 py-2 text-[11px] font-medium text-ink-secondary hover:border-ink-muted hover:text-ink">{file ? "重新选择" : "选择文件"}</button>
            </div>
            {file && <button type="button" onClick={() => { setFile(null); if (fileRef.current) fileRef.current.value = ""; }} className="mt-2 text-[10px] text-danger-600 hover:underline">移除文件</button>}
          </div>
          <footer className="flex justify-end gap-2 border-t border-line pt-4">
            <button type="button" onClick={onClose} disabled={saving} className="rounded-[8px] border border-line-strong bg-white px-4 py-2 text-[12px] text-ink-secondary disabled:opacity-40">取消</button>
            <button type="submit" disabled={saving} className="flex items-center gap-2 rounded-[8px] bg-ink px-4 py-2 text-[12px] font-medium text-white disabled:opacity-40">
              {saving && <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />}
              {file ? "上传并开始识别" : "创建资料"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
