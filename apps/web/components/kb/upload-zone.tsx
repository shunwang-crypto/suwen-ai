"use client";

// 上传区（F2）：拖拽 + 文件选择，multipart POST；后缀白名单提示；
// 进度由父组件轮询文档状态呈现，这里只负责把文件交给后端。

import { useCallback, useRef, useState, type DragEvent } from "react";

export const ALLOWED_SUFFIXES = [".pdf", ".docx", ".pptx", ".md", ".txt"];

export function UploadZone({
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
