"use client";

// 上传区（F2）：两个 tab —— 「本地文件」拖拽/选择 multipart POST；
// 「从网址导入」粘贴 URL 后端抓正文（对标 ima 网页剪藏）。
// 两条路径都只负责把内容交给后端，进度由父组件轮询文档状态呈现。

import { useCallback, useRef, useState, type DragEvent, type FormEvent } from "react";

export const ALLOWED_SUFFIXES = [".pdf", ".docx", ".pptx", ".md", ".txt"];

type Tab = "file" | "url";

function FileTab({
  uploading,
  onFiles,
}: {
  uploading: boolean;
  onFiles: (files: File[]) => void;
}) {
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragOver(false);
      const files = Array.from(e.dataTransfer.files ?? []);
      if (files.length > 0) onFiles(files);
    },
    [onFiles],
  );

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
      className={`flex flex-col items-center gap-2 rounded-2xl border-2 border-dashed px-4 py-8 text-center transition-colors ${
        dragOver
          ? "border-indigo-400 bg-indigo-50/60"
          : "border-slate-300 bg-white hover:border-indigo-300"
      }`}
    >
      <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-50 text-indigo-500">
        {uploading ? (
          <span className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-200 border-t-indigo-600" />
        ) : (
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-5 w-5"
            aria-hidden
          >
            <path d="M12 16V4m0 0 4 4m-4-4-4 4M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" />
          </svg>
        )}
      </span>
      <p className="text-sm text-slate-600">
        {uploading ? (
          "正在上传…"
        ) : (
          <>
            拖拽文件到此处，或
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              className="mx-1 font-medium text-indigo-600 hover:text-indigo-700"
            >
              点击选择文件
            </button>
          </>
        )}
      </p>
      <p className="text-xs text-slate-400">
        支持 {ALLOWED_SUFFIXES.join(" / ")}，上传后自动解析、切块并向量化
      </p>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ALLOWED_SUFFIXES.join(",")}
        className="hidden"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          if (files.length > 0) onFiles(files);
          e.target.value = "";
        }}
      />
    </div>
  );
}

function UrlTab({
  importing,
  onImportUrl,
}: {
  importing: boolean;
  onImportUrl: (url: string) => void;
}) {
  const [url, setUrl] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const value = url.trim();
    if (!value || importing) return;
    onImportUrl(value);
    setUrl("");
  };

  return (
    <form
      onSubmit={submit}
      className="flex flex-col gap-2 rounded-2xl border-2 border-dashed border-slate-300 bg-white px-4 py-6"
    >
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          disabled={importing}
          maxLength={2048}
          placeholder="粘贴网页链接，如 https://example.com/article"
          className="min-w-0 flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-50"
        />
        <button
          type="submit"
          disabled={!url.trim() || importing}
          className="flex shrink-0 items-center justify-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {importing && (
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
          )}
          {importing ? "抓取中…" : "导入"}
        </button>
      </div>
      <p className="text-xs text-slate-400">
        自动抽取网页正文转为 Markdown 后入库；仅支持 http/https 公网地址，内网与本机地址会被拒绝
      </p>
    </form>
  );
}

export function UploadZone({
  uploading,
  importing = false,
  onFiles,
  onImportUrl,
}: {
  uploading: boolean;
  importing?: boolean;
  onFiles: (files: File[]) => void;
  onImportUrl?: (url: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("file");
  const tabs: { key: Tab; label: string }[] = [
    { key: "file", label: "本地文件" },
    { key: "url", label: "从网址导入" },
  ];

  return (
    <div className="space-y-2">
      {onImportUrl && (
        <div className="flex gap-1 rounded-xl bg-slate-100 p-1">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              aria-pressed={tab === t.key}
              className={`flex-1 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors ${
                tab === t.key
                  ? "bg-white text-indigo-700 shadow-sm"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      )}
      {tab === "url" && onImportUrl ? (
        <UrlTab importing={importing} onImportUrl={onImportUrl} />
      ) : (
        <FileTab uploading={uploading} onFiles={onFiles} />
      )}
    </div>
  );
}
