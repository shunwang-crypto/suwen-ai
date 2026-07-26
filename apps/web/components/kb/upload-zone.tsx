"use client";

// 上传区（F2 + P2-17）：两个 tab —— 「本地文件」拖拽/选择上传；「从网址导入」
// 粘贴 URL 后端抓正文（对标 ima 网页剪藏）。
// P2-17 上传体验：
// - file.size 前端预校验：> MAX_UPLOAD_MB 直接提示，不发请求（不再白等 50 秒）；
// - 传入 uploadTo 时本组件自行上传并显示真实进度条；否则回落 onFiles 旧路径
//   （由父组件上传，无进度）。主线接线见文件末尾注释。

import { useCallback, useRef, useState, type DragEvent, type FormEvent } from "react";

import { FxStyles } from "@/components/common";
import { API_BASE, getAccessToken, refreshAccessToken } from "@/lib/api";

export const ALLOWED_SUFFIXES = [".pdf", ".docx", ".pptx", ".md", ".txt"];
/** 与后端 max_upload_mb 同值（config.py）；超限文件不发请求直接拦下。 */
export const MAX_UPLOAD_MB = 50;

type Tab = "file" | "url";

interface UploadProgress {
  filename: string;
  /** 0-100；null = 总大小未知（lengthComputable=false） */
  pct: number | null;
  index: number;
  total: number;
}

interface XhrResult {
  ok: boolean;
  status: number;
  message: string;
}

// fetch 不暴露上传进度事件，仅此组件改用 XMLHttpRequest 以拿到 upload.onprogress。
function xhrUpload(
  url: string,
  file: File,
  onProgress: (pct: number | null) => void,
): Promise<XhrResult> {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    const token = getAccessToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => {
      onProgress(e.lengthComputable ? Math.round((e.loaded / e.total) * 100) : null);
    };
    xhr.onload = () => {
      let message = "";
      try {
        const body = JSON.parse(xhr.responseText || "{}") as {
          message?: string;
          code?: string;
        };
        message = body.message || body.code || "";
      } catch {
        // 非 JSON 响应体：保留空串，走兜底文案
      }
      resolve({
        ok: xhr.status >= 200 && xhr.status < 300,
        status: xhr.status,
        message: message || (xhr.status >= 400 ? `上传失败（HTTP ${xhr.status}）` : ""),
      });
    };
    xhr.onerror = () =>
      resolve({ ok: false, status: 0, message: "网络异常，上传失败" });
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

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
      className={`relative flex flex-col items-center gap-2 overflow-hidden rounded-2xl border-2 border-dashed px-4 py-8 text-center transition-colors ${
        dragOver
          ? "border-indigo-400 bg-indigo-50/60"
          : "border-slate-300 bg-white hover:border-indigo-300"
      }`}
    >
      <FxStyles />
      {/* 拖入时的边框流光（reduced-motion 下静态高亮边框兜底） */}
      {dragOver && (
        <span
          aria-hidden
          className="edu-flow-x pointer-events-none absolute inset-0 rounded-2xl"
          style={{
            background:
              "linear-gradient(110deg, rgba(99,102,241,0) 25%, rgba(99,102,241,0.22) 50%, rgba(99,102,241,0) 75%)",
          }}
        />
      )}
      <span className="relative flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-50 text-indigo-500">
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
        支持 {ALLOWED_SUFFIXES.join(" / ")}，单文件不超过 {MAX_UPLOAD_MB}MB，
        上传后自动解析、切块并向量化
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
  uploadTo,
  onUploaded,
}: {
  uploading: boolean;
  importing?: boolean;
  onFiles: (files: File[]) => void;
  onImportUrl?: (url: string) => void;
  /**
   * P2-17：传入文档上传端点（如 `/api/kb/{kbId}/documents`）时，本组件用 XHR
   * 自行上传并显示真实进度；不传则回落 onFiles（父组件上传，无进度）。
   */
  uploadTo?: string;
  /** XHR 路径全部文件处理完后回调（父组件刷新文档列表用）。 */
  onUploaded?: (failures: string[]) => void;
}) {
  const [tab, setTab] = useState<Tab>("file");
  const [xhrBusy, setXhrBusy] = useState(false);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const tabs: { key: Tab; label: string }[] = [
    { key: "file", label: "本地文件" },
    { key: "url", label: "从网址导入" },
  ];

  const uploadViaXhr = useCallback(
    async (files: File[], failures: string[]) => {
      if (!uploadTo) return;
      setXhrBusy(true);
      try {
        for (let i = 0; i < files.length; i += 1) {
          const file = files[i];
          const report = (pct: number | null) =>
            setProgress({ filename: file.name, pct, index: i + 1, total: files.length });
          report(0);
          let result = await xhrUpload(`${API_BASE}${uploadTo}`, file, report);
          if (result.status === 401 && (await refreshAccessToken())) {
            result = await xhrUpload(`${API_BASE}${uploadTo}`, file, report);
          }
          if (!result.ok) failures.push(`${file.name}：${result.message}`);
        }
      } finally {
        setXhrBusy(false);
        setProgress(null);
      }
      if (failures.length > 0) setLocalError(failures.join("；"));
      onUploaded?.(failures);
    },
    [uploadTo, onUploaded],
  );

  /** 体积预校验（P2-17）：超限文件直接提示、不发请求；其余按模式分发。 */
  const handleFiles = useCallback(
    (files: File[]) => {
      setLocalError(null);
      const oversized = files.filter((f) => f.size > MAX_UPLOAD_MB * 1024 * 1024);
      const valid = files.filter((f) => f.size <= MAX_UPLOAD_MB * 1024 * 1024);
      const failures = oversized.map(
        (f) =>
          `${f.name}：${(f.size / 1024 / 1024).toFixed(1)}MB 超过 ${MAX_UPLOAD_MB}MB 上限，未上传`,
      );
      if (failures.length > 0) setLocalError(failures.join("；"));
      if (valid.length === 0) {
        if (failures.length > 0) onUploaded?.(failures);
        return;
      }
      if (uploadTo) void uploadViaXhr(valid, failures);
      else onFiles(valid);
    },
    [uploadTo, uploadViaXhr, onFiles, onUploaded],
  );

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
        <>
          <FileTab uploading={uploading || xhrBusy} onFiles={handleFiles} />
          {progress && (
            <div className="rounded-xl border border-indigo-100 bg-indigo-50/50 px-3 py-2">
              <div className="mb-1 flex items-baseline justify-between gap-2 text-xs">
                <span className="min-w-0 truncate text-slate-600">
                  {progress.filename}
                  {progress.total > 1 && (
                    <span className="text-slate-400">
                      （{progress.index}/{progress.total}）
                    </span>
                  )}
                </span>
                <span className="shrink-0 font-mono font-semibold text-indigo-700">
                  {progress.pct != null ? `${progress.pct}%` : "上传中…"}
                </span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-indigo-100">
                <div
                  className={`h-full rounded-full bg-indigo-500 transition-[width] duration-200 ${
                    progress.pct == null ? "w-1/3 animate-pulse" : ""
                  }`}
                  style={progress.pct != null ? { width: `${progress.pct}%` } : undefined}
                />
              </div>
            </div>
          )}
          {localError && (
            <p className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {localError}
            </p>
          )}
        </>
      )}
    </div>
  );
}

// 主线接线（kb/page.tsx，两个属性即可启用进度条模式；不接则行为与旧版一致）：
//   <UploadZone ... uploadTo={selectedId ? `/api/kb/${selectedId}/documents` : undefined}
//               onUploaded={() => void loadDocs(selectedId!, true)} />
