"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Spinner } from "@/components/common";
import { ApiError, apiFetch, apiJson } from "@/lib/api";
import type { TutorProblemDraft } from "@/lib/types";

const ACCEPT = ".png,.jpg,.jpeg,.webp,.pdf,.docx,.pptx";
const STATUS_LABEL: Record<TutorProblemDraft["status"], string> = {
  uploaded: "已上传",
  recognizing: "解析/识别中",
  review: "待校对",
  confirmed: "已确认",
  failed: "识别失败",
};
const WARNING_LABEL: Record<string, string> = {
  EMBEDDED_IMAGES_NOT_RECOGNIZED: "文件中的嵌入图片未能提取文字，请对照原文件手工补充。",
  VISION_MODEL_STUB_MANUAL_ENTRY_REQUIRED: "当前环境未启用视觉识别，请手工填写题干。",
  SCANNED_PAGES_REQUIRE_VISION: "所选扫描页需要视觉识别，请重试或手工填写。",
};

interface Props {
  initialProblem?: string;
  initialDraftId?: string | null;
  locked?: boolean;
  onDraftChange?: (draftId: string | null) => void;
  onConfirmed: (draft: TutorProblemDraft) => void;
}

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return "附件处理失败，请稍后重试";
  const messages: Record<string, string> = {
    MODEL_REQUEST_INVALID: "当前模型无法处理该文件，请重试或稍后再试。",
    VISION_ROUTE_UNAVAILABLE: "图片识别服务暂时不可用。",
    PDF_PASSWORD_PROTECTED: "PDF 已加密，请解除密码后重新上传。",
    PDF_PAGE_LIMIT_EXCEEDED: "PDF 页数超过限制。",
    PDF_PARTIAL_PAGE_FAILURE: "部分页面未能识别，请检查失败页。",
    NO_QUESTIONS_RECOGNIZED: "没有识别出题目，可手动输入题目。",
    TUTOR_ATTACHMENT_NOT_FOUND: "原文件暂时不可用，请重新选择附件。",
  };
  return messages[error.code] ?? "附件处理失败，请稍后重试";
}

export function TutorProblemInput({
  initialProblem = "",
  initialDraftId = null,
  locked = false,
  onDraftChange,
  onConfirmed,
}: Props) {
  const [draft, setDraft] = useState<TutorProblemDraft | null>(null);
  const [stem, setStem] = useState(initialProblem);
  const [options, setOptions] = useState<string[]>([]);
  const [knownConditions, setKnownConditions] = useState("");
  const [currentThought, setCurrentThought] = useState("");
  const [selectedPages, setSelectedPages] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const reviewButtonRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const onDraftChangeRef = useRef(onDraftChange);

  useEffect(() => {
    onDraftChangeRef.current = onDraftChange;
  }, [onDraftChange]);

  const applyDraft = useCallback((value: TutorProblemDraft) => {
    setDraft(value);
    setStem(value.stem || value.confirmed_problem_text);
    setOptions(value.options ?? []);
    setKnownConditions(value.known_conditions ?? "");
    setCurrentThought(value.current_thought ?? "");
    setSelectedPages(value.selected_pages ?? []);
    onDraftChangeRef.current?.(value.id);
  }, []);

  useEffect(() => {
    if (!initialDraftId) return;
    let active = true;
    void apiJson<TutorProblemDraft>(`/api/tutor/drafts/${initialDraftId}`)
      .then((value) => {
        if (active) applyDraft(value);
      })
      .catch((caught) => {
        if (active) setError(errorMessage(caught));
      });
    return () => {
      active = false;
    };
  }, [applyDraft, initialDraftId]);

  useEffect(() => {
    if (draft?.status !== "recognizing") return;
    const timer = window.setInterval(() => {
      void apiJson<TutorProblemDraft>(`/api/tutor/drafts/${draft.id}`)
        .then((value) => {
          applyDraft(value);
          if (value.status === "review") setReviewOpen(true);
        })
        .catch((caught) => setError(errorMessage(caught)));
    }, 1200);
    return () => window.clearInterval(timer);
  }, [applyDraft, draft?.id, draft?.status]);

  useEffect(() => {
    if (!draft?.attachment || !draft.attachment.mime_type.startsWith("image/")) return;
    let active = true;
    let objectUrl: string | null = null;
    void apiFetch(draft.attachment.preview_url)
      .then((response) => response.blob())
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setPreviewUrl(objectUrl);
      })
      .catch(() => undefined);
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [draft?.attachment]);

  useEffect(() => {
    if (!reviewOpen) return;
    openerRef.current = document.activeElement as HTMLElement | null;
    const fallbackOpener = reviewButtonRef.current;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setReviewOpen(false);
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href]',
        ),
      );
      if (!focusable.length) return;
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
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      const opener = openerRef.current;
      if (
        opener !== null &&
        opener === fallbackOpener &&
        opener.isConnected &&
        opener.tabIndex >= 0
      ) {
        opener.focus();
      } else {
        // Recognition may open the dialog automatically after a file picker,
        // leaving body as the active element.  Restore to a deterministic
        // actionable control in that case.
        fallbackOpener?.focus();
      }
    };
  }, [reviewOpen]);

  const upload = async (file: File) => {
    const suffix = `.${file.name.split(".").pop()?.toLowerCase() ?? ""}`;
    if (!ACCEPT.split(",").includes(suffix)) {
      setError("仅支持 PNG、JPG、JPEG、WebP、PDF、DOCX 和 PPTX");
      return;
    }
    setBusy(true);
    setError(null);
    const form = new FormData();
    form.append("file", file, file.name || `clipboard-${Date.now()}.png`);
    try {
      const response = await apiFetch("/api/tutor/drafts", { method: "POST", body: form });
      const value = (await response.json()) as TutorProblemDraft;
      applyDraft(value);
      if (value.status === "review") setReviewOpen(true);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const recognize = async () => {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      const recognized = await apiJson<TutorProblemDraft>(`/api/tutor/drafts/${draft.id}/recognize`, {
        body: { pages: selectedPages },
      });
      applyDraft(recognized);
      if (recognized.status === "review") setReviewOpen(true);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const chooseQuestion = async (index: number) => {
    if (!draft) return;
    setBusy(true);
    try {
      applyDraft(await apiJson<TutorProblemDraft>(`/api/tutor/drafts/${draft.id}`, {
        method: "PATCH",
        body: { selected_question_index: index },
      }));
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!stem.trim()) {
      setError("请填写并校对题干");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      let activeDraft = draft;
      if (!activeDraft) {
        activeDraft = await apiJson<TutorProblemDraft>("/api/tutor/drafts/manual", {
          body: { stem, options, known_conditions: knownConditions, current_thought: currentThought },
        });
      }
      const confirmed = await apiJson<TutorProblemDraft>(`/api/tutor/drafts/${activeDraft.id}/confirm`, {
        body: { stem, options, known_conditions: knownConditions, current_thought: currentThought },
      });
      applyDraft(confirmed);
      setReviewOpen(false);
      onConfirmed(confirmed);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!draft || draft.status === "confirmed") return;
    setBusy(true);
    try {
      await apiJson<void>(`/api/tutor/drafts/${draft.id}`, { method: "DELETE" });
      setDraft(null);
      setPreviewUrl(null);
      setSelectedPages([]);
      onDraftChange?.(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const openPreview = async () => {
    if (!draft?.attachment) return;
    if (!previewUrl) {
      try {
        const response = await apiFetch(draft.attachment.preview_url);
        setPreviewUrl(URL.createObjectURL(await response.blob()));
      } catch (caught) {
        setError(errorMessage(caught));
        return;
      }
    }
    setPreviewOpen((value) => !value);
  };

  const pages = Array.from({ length: draft?.attachment?.page_count ?? 0 }, (_, index) => index + 1);
  const isPdf = draft?.attachment?.mime_type === "application/pdf";

  return (
    <div
      className={`mt-3 rounded-[9px] border ${dragging ? "border-brand-400 bg-brand-50" : "border-line-strong bg-[#fafaf9]"}`}
      onDragEnter={(event) => { event.preventDefault(); if (!locked) setDragging(true); }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false); }}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        if (!locked && event.dataTransfer.files[0]) void upload(event.dataTransfer.files[0]);
      }}
    >
      {!locked && (
        <textarea
          aria-label="题目题干"
          value={stem}
          onChange={(event) => setStem(event.target.value)}
          onPaste={(event) => {
            const image = Array.from(event.clipboardData.files).find((file) => file.type.startsWith("image/"));
            if (image) {
              event.preventDefault();
              const named = image.name ? image : new File([image], `clipboard-${Date.now()}.png`, { type: image.type || "image/png" });
              void upload(named);
            }
          }}
          rows={3}
          maxLength={12000}
          placeholder="粘贴题目文字或剪贴板图片，也可选择、拖拽题目文件"
          className="w-full resize-y border-0 bg-transparent px-3 py-2.5 text-[13px] leading-6 text-ink outline-none placeholder:text-ink-muted"
        />
      )}
      {locked && (
        <div className="whitespace-pre-wrap px-3 py-2.5 text-[13px] font-medium leading-6 text-ink">
          {draft?.confirmed_problem_text || stem}
        </div>
      )}

      {draft?.attachment && (
        <div className="mx-3 mb-3 flex min-w-0 items-center gap-3 border border-line bg-white p-2.5">
          {previewUrl && draft.attachment.mime_type.startsWith("image/") ? (
            // Authenticated object URL, not Base64 and never persisted.
            // eslint-disable-next-line @next/next/no-img-element
            <img src={previewUrl} alt="题目附件缩略图" className="h-12 w-12 shrink-0 object-cover" />
          ) : (
            <span className="flex h-12 w-12 shrink-0 items-center justify-center bg-surface-muted text-[10px] font-semibold text-ink-muted">
              {draft.attachment.filename.split(".").pop()?.toUpperCase()}
            </span>
          )}
          <div className="min-w-0 flex-1">
            <p className="truncate text-xs font-medium text-ink">{draft.attachment.filename}</p>
            <p className="mt-1 flex items-center gap-1.5 text-[10px] text-ink-muted">
              {draft.status === "recognizing" && <Spinner className="h-3 w-3" />}
              <span>{STATUS_LABEL[draft.status]}</span>
              {draft.confirmed_page_no ? ` · 第 ${draft.confirmed_page_no} 页` : ""}
              {draft.attachment.page_count ? ` · 共 ${draft.attachment.page_count} 页` : ""}
            </p>
          </div>
          <button type="button" onClick={() => void openPreview()} className="h-8 w-8 text-sm text-ink-secondary" aria-label={previewOpen ? "收起原文件" : "预览原文件"} title="预览原文件">⌕</button>
          {!locked && <button type="button" disabled={busy} onClick={() => void remove()} className="h-8 w-8 text-base text-danger-600 disabled:opacity-40" aria-label="删除附件" title="删除附件">×</button>}
        </div>
      )}

      {previewOpen && previewUrl && draft?.attachment && (
        <div className="mx-3 mb-3 overflow-hidden border border-line bg-white">
          {draft.attachment.mime_type.startsWith("image/") ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={previewUrl} alt="原始题目附件" className="max-h-[420px] w-full object-contain" />
          ) : draft.attachment.mime_type === "application/pdf" ? (
            <iframe src={previewUrl} title="原始 PDF" className="h-[420px] w-full" />
          ) : (
            <div className="p-4 text-xs text-ink-secondary">浏览器无法直接预览该 Office 文件。<a href={previewUrl} download={draft.attachment.filename} className="ml-2 font-medium text-brand-700">下载原文件</a></div>
          )}
        </div>
      )}

      {!locked && isPdf && draft?.status !== "recognizing" && draft?.status !== "confirmed" && (
        <div className="mx-3 mb-3 flex items-center justify-between border-t border-line pt-3 text-[10px] text-ink-secondary">
          <span>{selectedPages.length ? `已选择 ${selectedPages.length} 页` : "PDF 页面将在校对时选择"}</span>
          <button type="button" onClick={() => setReviewOpen(true)} className="border border-line-strong px-2.5 py-1.5">选择页面</button>
        </div>
      )}

      {draft?.vision_uses_platform_quota && !locked && (
        <p className="mx-3 mb-2 text-[10px] leading-5 text-warning-700">图片和扫描 PDF 的视觉识别使用平台模型，可能消耗平台额度；不会使用你的 DeepSeek Key。</p>
      )}
      {draft?.warnings.map((warning) => <p key={warning} className="mx-3 mb-2 text-[10px] text-warning-700">{WARNING_LABEL[warning] ?? "部分内容需要人工检查，请对照原文件校对。"}</p>)}
      {draft?.status === "failed" && (
        <div className="mx-3 mb-2 rounded-[8px] border border-danger-200 bg-danger-50 px-3 py-2 text-[11px] text-danger-700">
          <p>暂时无法识别该文件，原文件已经保留。</p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" disabled={busy} onClick={() => void recognize()} className="border border-danger-300 bg-white px-2.5 py-1.5">重新识别</button>
            {isPdf && <button type="button" onClick={() => setReviewOpen(true)} className="border border-danger-300 bg-white px-2.5 py-1.5">重新选择页面</button>}
            <button type="button" onClick={() => { setDraft(null); setStem(""); }} className="border border-danger-300 bg-white px-2.5 py-1.5">手动输入题目</button>
          </div>
        </div>
      )}
      {error && draft?.status !== "failed" && <p role="alert" className="mx-3 mb-2 text-[11px] text-danger-600">{error}</p>}

      {!locked && (
        <div className="flex flex-wrap items-center gap-2 border-t border-line px-3 py-2.5">
          <input ref={fileInputRef} type="file" accept={ACCEPT} className="sr-only" onChange={(event) => { if (event.target.files?.[0]) void upload(event.target.files[0]); }} />
          <button type="button" disabled={busy} onClick={() => fileInputRef.current?.click()} className="flex h-8 w-8 items-center justify-center text-base text-ink-secondary disabled:opacity-40" aria-label="添加题目附件" title="添加题目附件">⌕</button>
          <span className="text-[10px] text-ink-muted">拖拽文件，或粘贴剪贴板图片</span>
          <span className="flex-1" />
          {draft?.status === "failed" && <button type="button" disabled={busy} onClick={() => void recognize()} className="border border-line-strong px-3 py-1.5 text-[11px] text-ink-secondary">重试识别</button>}
          <button ref={reviewButtonRef} type="button" disabled={busy || draft?.status === "recognizing" || draft?.status === "uploaded" || !stem.trim()} onClick={() => setReviewOpen(true)} className="bg-ink px-3 py-1.5 text-[11px] font-medium text-white disabled:opacity-40">{draft?.status === "review" ? "校对题目" : "校对并确认"}</button>
          {busy && <Spinner className="h-3.5 w-3.5" />}
        </div>
      )}

      {reviewOpen && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 sm:items-center" onMouseDown={(event) => { if (event.currentTarget === event.target) setReviewOpen(false); }}>
          <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="tutor-review-title" className="max-h-[92vh] w-full overflow-y-auto bg-white p-5 shadow-xl sm:max-w-2xl sm:rounded-[8px]">
            <div className="flex items-center justify-between gap-4">
              <h2 id="tutor-review-title" className="text-sm font-semibold text-ink">校对并选择本次辅导题目</h2>
              <button ref={closeRef} type="button" onClick={() => setReviewOpen(false)} className="h-8 w-8 text-lg text-ink-secondary" aria-label="关闭校对">×</button>
            </div>
            {isPdf && draft?.status !== "confirmed" && (
              <fieldset className="mt-4 rounded-[8px] border border-line bg-surface-muted p-3">
                <legend className="px-1 text-xs font-medium text-ink-secondary">选择本次识别页</legend>
                <div className="mt-2 flex max-h-28 flex-wrap gap-1.5 overflow-y-auto">
                  {pages.map((page) => (
                    <label key={page} className="flex items-center gap-1 border border-line bg-white px-2 py-1 text-[10px] text-ink-secondary">
                      <input type="checkbox" checked={selectedPages.includes(page)} onChange={(event) => setSelectedPages((current) => event.target.checked ? [...current, page].sort((a, b) => a - b) : current.filter((value) => value !== page))} />
                      第 {page} 页
                    </label>
                  ))}
                </div>
                {draft.status !== "review" && <button type="button" disabled={busy || selectedPages.length === 0} onClick={() => void recognize()} className="mt-3 bg-brand-600 px-3 py-2 text-[11px] font-medium text-white disabled:opacity-40">识别所选页面</button>}
              </fieldset>
            )}
            {draft && draft.questions.length > 1 && (
              <fieldset className="mt-4">
                <legend className="text-xs font-medium text-ink-secondary">文件中识别到 {draft.questions.length} 道题</legend>
                <div className="mt-2 max-h-44 space-y-2 overflow-y-auto">
                  {draft.questions.map((question) => (
                    <label key={question.index} className="flex cursor-pointer items-start gap-2 border border-line p-3 text-xs text-ink">
                      <input type="radio" name="tutor-question" checked={draft.selected_question_index === question.index} onChange={() => void chooseQuestion(question.index)} />
                      <span className="min-w-0"><span className="font-medium">第 {question.question_no} 题{question.page_no ? ` · 第 ${question.page_no} 页` : ""}</span><span className="mt-1 block line-clamp-2 text-ink-secondary">{question.stem}</span></span>
                    </label>
                  ))}
                </div>
              </fieldset>
            )}
            <label className="mt-4 block text-xs font-medium text-ink-secondary">题干<textarea value={stem} onChange={(event) => setStem(event.target.value)} rows={5} maxLength={12000} className="mt-2 w-full resize-y border border-line-strong p-3 text-sm font-normal leading-6 text-ink outline-none focus:border-brand-400" /></label>
            <fieldset className="mt-4"><legend className="text-xs font-medium text-ink-secondary">选项</legend><div className="mt-2 space-y-2">{options.map((option, index) => <div key={index} className="flex gap-2"><span className="w-5 pt-2 text-xs text-ink-muted">{String.fromCharCode(65 + index)}</span><input value={option} onChange={(event) => setOptions((current) => current.map((value, item) => item === index ? event.target.value : value))} maxLength={1000} className="min-w-0 flex-1 border border-line-strong px-3 py-2 text-sm" /><button type="button" onClick={() => setOptions((current) => current.filter((_, item) => item !== index))} className="h-9 w-9 text-danger-600" aria-label={`删除选项 ${String.fromCharCode(65 + index)}`}>×</button></div>)}</div>{options.length < 8 && <button type="button" onClick={() => setOptions((current) => [...current, ""])} className="mt-2 border border-line-strong px-3 py-1.5 text-xs text-ink-secondary">添加选项</button>}</fieldset>
            <label className="mt-4 block text-xs font-medium text-ink-secondary">已知条件<textarea value={knownConditions} onChange={(event) => setKnownConditions(event.target.value)} rows={2} maxLength={8000} className="mt-2 w-full resize-y border border-line-strong p-3 text-sm font-normal" /></label>
            <label className="mt-4 block text-xs font-medium text-ink-secondary">你当前的思路或卡点<textarea value={currentThought} onChange={(event) => setCurrentThought(event.target.value)} rows={3} maxLength={8000} className="mt-2 w-full resize-y border border-line-strong p-3 text-sm font-normal" /></label>
            <div className="mt-5 flex justify-end gap-2 border-t border-line pt-4"><button type="button" onClick={() => setReviewOpen(false)} className="border border-line-strong px-4 py-2 text-xs text-ink-secondary">返回修改</button><button type="button" disabled={busy || !stem.trim()} onClick={() => void confirm()} className="bg-brand-600 px-4 py-2 text-xs font-medium text-white disabled:opacity-40">确认并开始辅导</button></div>
          </div>
        </div>
      )}
    </div>
  );
}
