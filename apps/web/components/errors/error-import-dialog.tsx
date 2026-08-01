"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";

import { Spinner } from "@/components/common";
import { ApiError, apiFetch, apiJson } from "@/lib/api";
import type {
  ErrorImportCapabilities,
  ErrorImportDraft,
  ErrorImportOut,
  ErrorItemOut,
  KbOut,
} from "@/lib/types";

const TYPE_OPTIONS = [
  ["single", "单选题"],
  ["multi", "多选题"],
  ["blank", "填空题"],
  ["short", "解答题"],
] as const;

const DIFFICULTY_OPTIONS = [
  ["easy", "基础"],
  ["mid", "中等"],
  ["hard", "较难"],
] as const;

const CAUSE_OPTIONS = [
  ["knowledge_gap", "知识缺口"],
  ["concept_confusion", "概念混淆"],
  ["formula_error", "公式用错"],
  ["calc_slip", "计算失误"],
  ["misread", "审题偏差"],
] as const;

type DraftForm = ErrorImportDraft & {
  kb_id: string;
  cause: string;
  answer_verified: boolean;
};

const EMPTY_FORM: DraftForm = {
  type: "short",
  stem: "",
  options: [],
  my_answer: "",
  answer: "",
  explanation: "",
  kc_names: [],
  difficulty: "mid",
  kb_id: "",
  cause: "knowledge_gap",
  answer_verified: false,
};

const fieldClass =
  "w-full rounded-[8px] border border-line-strong bg-white px-3 py-2 text-[12px] text-ink outline-none placeholder:text-ink-muted focus:border-ink-muted";

function fromItem(item: ErrorItemOut): DraftForm {
  const sourceKb = item.question?.source_meta?.kb_id;
  return {
    type: (item.question?.type as DraftForm["type"]) || "short",
    stem: item.question?.stem ?? "",
    options: item.question?.options ?? [],
    my_answer: item.my_answer,
    answer: item.question?.answer ?? "",
    explanation: item.question?.explanation ?? "",
    kc_names: item.kc_names,
    difficulty: (item.question?.difficulty as DraftForm["difficulty"]) || "mid",
    kb_id: typeof sourceKb === "string" ? sourceKb : "",
    cause: item.cause,
    answer_verified: item.verification_status === "verified",
  };
}

export function ErrorImportDialog({
  editItem,
  knowledgeBases,
  onClose,
  onSaved,
}: {
  editItem?: ErrorItemOut | null;
  knowledgeBases: KbOut[];
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<"upload" | "manual">(editItem ? "manual" : "upload");
  const [form, setForm] = useState<DraftForm>(() =>
    editItem ? fromItem(editItem) : EMPTY_FORM,
  );
  const [imported, setImported] = useState<ErrorImportOut | null>(null);
  const [capabilities, setCapabilities] = useState<ErrorImportCapabilities | null>(null);
  const [uploading, setUploading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (editItem) return;
    void apiJson<ErrorImportCapabilities>("/api/errors/imports/capabilities")
      .then(setCapabilities)
      .catch(() => setCapabilities(null));
  }, [editItem]);

  const set = <K extends keyof DraftForm>(key: K, value: DraftForm[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const upload = async (file: File) => {
    setUploading(true);
    setError(null);
    try {
      const data = new FormData();
      data.append("file", file);
      const response = await apiFetch("/api/errors/imports", { method: "POST", body: data });
      const result = (await response.json()) as ErrorImportOut;
      setImported(result);
      setForm((prev) => ({
        ...prev,
        ...result.draft,
        answer_verified: false,
      }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "文件识别失败，请重试或手动录入");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!form.stem.trim()) {
      setError("请填写题目内容");
      return;
    }
    if (form.answer_verified && !form.answer.trim()) {
      setError("请先填写正确答案，再确认答案已经核验");
      return;
    }
    if ((form.type === "single" || form.type === "multi") && form.options.length < 2) {
      setError("选择题至少需要两个选项，每行填写一个选项");
      return;
    }
    setSaving(true);
    setError(null);
    const payload = {
      ...form,
      stem: form.stem.trim(),
      answer: form.answer.trim(),
      my_answer: form.my_answer.trim(),
      explanation: form.explanation.trim(),
      kc_names: form.kc_names.map((item) => item.trim()).filter(Boolean).slice(0, 3),
      kb_id: form.kb_id || null,
    };
    try {
      if (editItem) {
        await apiJson(`/api/errors/${editItem.id}/content`, {
          method: "PATCH",
          body: payload,
        });
      } else {
        let importId = imported?.id;
        if (!importId) {
          const created = await apiJson<ErrorImportOut>("/api/errors/imports/manual", {
            method: "POST",
            body: {},
          });
          importId = created.id;
        }
        await apiJson(`/api/errors/imports/${importId}/confirm`, { body: payload });
      }
      onSaved(
        form.answer_verified
          ? editItem
            ? "题目与答案已核验，已加入复习队列。"
            : "错题已收录，并加入复习队列。"
          : "错题已保存为待核验，不会影响掌握度或进入复习。",
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败，请稍后重试");
    } finally {
      setSaving(false);
    }
  };

  const accept = [
    ...(capabilities?.image_suffixes ?? [".png", ".jpg", ".jpeg", ".webp"]),
    ...(capabilities?.document_suffixes ?? [".pdf", ".docx", ".pptx", ".md", ".txt"]),
  ].join(",");

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-[#1c1917]/40"
        onClick={() => !saving && !uploading && onClose()}
        aria-label="关闭错题录入"
      />
      <section className="relative flex max-h-[calc(100dvh-32px)] w-full max-w-[720px] flex-col overflow-hidden rounded-[12px] border border-line bg-canvas shadow-floating">
        <header className="flex items-start justify-between border-b border-line bg-white px-5 py-4">
          <div>
            <h2 className="text-[17px] font-semibold text-ink">
              {editItem ? "编辑并核验错题" : "录入错题"}
            </h2>
            <p className="mt-1 text-[11px] text-ink-muted">
              识别内容只作为草稿，保存前请核对题干和答案。
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving || uploading}
            className="rounded-[7px] p-2 text-ink-muted hover:bg-surface-muted disabled:opacity-40"
            aria-label="关闭"
          >
            ✕
          </button>
        </header>

        <form id="error-import-form" onSubmit={submit} className="min-h-0 flex-1 overflow-y-auto p-5">
          {!editItem && (
            <div className="mb-5">
              <div className="mb-3 flex w-fit rounded-[8px] border border-line bg-white p-1">
                <button
                  type="button"
                  onClick={() => setMode("upload")}
                  className={`rounded-[6px] px-3 py-1.5 text-[11px] font-medium ${mode === "upload" ? "bg-ink text-white" : "text-ink-secondary"}`}
                >
                  拍照或上传
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setMode("manual");
                    setImported(null);
                    setForm(EMPTY_FORM);
                  }}
                  className={`rounded-[6px] px-3 py-1.5 text-[11px] font-medium ${mode === "manual" ? "bg-ink text-white" : "text-ink-secondary"}`}
                >
                  手动录入
                </button>
              </div>
              {mode === "upload" && (
                <div className="rounded-[10px] border border-dashed border-line-strong bg-white p-4">
                  <input
                    ref={fileRef}
                    type="file"
                    className="sr-only"
                    accept={accept}
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void upload(file);
                    }}
                  />
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <p className="text-[12px] font-medium text-ink">
                        {imported?.filename ?? "选择一道题的图片或文件"}
                      </p>
                      <p className="mt-1 text-[10px] text-ink-muted">
                        支持图片、PDF、Word、PPT；单个文件不超过 {capabilities?.max_file_mb ?? 20}MB
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={uploading}
                      onClick={() => fileRef.current?.click()}
                      className="flex h-9 items-center gap-2 rounded-[8px] bg-ink px-4 text-[11px] font-medium text-white disabled:opacity-40"
                    >
                      {uploading && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}
                      {uploading ? "正在识别" : imported ? "重新选择" : "选择文件"}
                    </button>
                  </div>
                  {imported?.recognition_warning && (
                    <p className="mt-3 rounded-[7px] bg-warning-50 px-3 py-2 text-[10px] text-warning-700">
                      {imported.recognition_warning}
                    </p>
                  )}
                </div>
              )}
            </div>
          )}

          {error && (
            <div className="mb-4 rounded-[8px] border border-danger-200 bg-danger-50 px-3 py-2 text-[11px] text-danger-700">
              {error}
            </div>
          )}

          <div className="grid gap-4 sm:grid-cols-3">
            <label>
              <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">题型</span>
              <select value={form.type} onChange={(event) => set("type", event.target.value as DraftForm["type"])} className={fieldClass}>
                {TYPE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label>
              <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">难度</span>
              <select value={form.difficulty} onChange={(event) => set("difficulty", event.target.value as DraftForm["difficulty"])} className={fieldClass}>
                {DIFFICULTY_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label>
              <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">错因</span>
              <select value={form.cause} onChange={(event) => set("cause", event.target.value)} className={fieldClass}>
                {CAUSE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
          </div>

          <label className="mt-4 block">
            <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">题目 <span className="text-danger-600">必填</span></span>
            <textarea value={form.stem} onChange={(event) => set("stem", event.target.value)} rows={4} maxLength={12000} placeholder="完整输入题干和条件" className={`${fieldClass} resize-y leading-5`} />
          </label>

          {(form.type === "single" || form.type === "multi") && (
            <label className="mt-4 block">
              <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">选项 <span className="text-danger-600">必填</span></span>
              <textarea
                value={form.options.join("\n")}
                onChange={(event) => set("options", event.target.value.split("\n"))}
                rows={4}
                placeholder={"A. 第一个选项\nB. 第二个选项"}
                className={`${fieldClass} resize-y font-mono leading-5`}
              />
            </label>
          )}

          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label>
              <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">我的错误答案 <span className="text-ink-muted">选填</span></span>
              <textarea value={form.my_answer} onChange={(event) => set("my_answer", event.target.value)} rows={3} maxLength={8000} placeholder="当时写下的答案或解题过程" className={`${fieldClass} resize-y`} />
            </label>
            <label>
              <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">正确答案 <span className="text-ink-muted">可稍后核验</span></span>
              <textarea value={form.answer} onChange={(event) => set("answer", event.target.value)} rows={3} maxLength={8000} placeholder="不确定时可以暂时留空" className={`${fieldClass} resize-y`} />
            </label>
          </div>

          <label className="mt-4 block">
            <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">解析 <span className="text-ink-muted">选填</span></span>
            <textarea value={form.explanation} onChange={(event) => set("explanation", event.target.value)} rows={3} maxLength={12000} placeholder="记录老师讲解或自己的订正思路" className={`${fieldClass} resize-y`} />
          </label>

          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label>
              <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">关联资料范围 <span className="text-ink-muted">选填</span></span>
              <select value={form.kb_id} onChange={(event) => set("kb_id", event.target.value)} className={fieldClass}>
                <option value="">不关联知识库</option>
                {knowledgeBases.map((kb) => <option key={kb.id} value={kb.id}>{kb.name}</option>)}
              </select>
            </label>
            <label>
              <span className="mb-1.5 block text-[10px] font-medium text-ink-secondary">知识点 <span className="text-ink-muted">最多 3 个</span></span>
              <input value={form.kc_names.join("，")} onChange={(event) => set("kc_names", event.target.value.split(/[,，]/))} placeholder="例如：极限，洛必达法则" className={fieldClass} />
            </label>
          </div>

          <label className="mt-5 flex items-start gap-3 rounded-[9px] border border-line bg-white p-3">
            <input
              type="checkbox"
              checked={form.answer_verified}
              onChange={(event) => set("answer_verified", event.target.checked)}
              className="mt-0.5 h-4 w-4 accent-ink"
            />
            <span>
              <span className="block text-[11px] font-medium text-ink">我已核对正确答案</span>
              <span className="mt-0.5 block text-[10px] leading-4 text-ink-muted">
                未勾选时保存为待核验，不进入今日复习，也不会影响学习画像。
              </span>
            </span>
          </label>
        </form>

        <footer className="flex items-center justify-between gap-3 border-t border-line bg-white px-5 py-4">
          <p className="text-[10px] text-ink-muted">上传文件不会自动加入知识库。</p>
          <div className="flex items-center gap-2">
            <button type="button" onClick={onClose} disabled={saving || uploading} className="h-9 rounded-[8px] border border-line-strong px-4 text-[11px] font-medium text-ink-secondary disabled:opacity-40">取消</button>
            <button type="submit" form="error-import-form" disabled={saving || uploading} className="flex h-9 items-center gap-2 rounded-[8px] bg-brand-600 px-4 text-[11px] font-medium text-white disabled:opacity-40">
              {saving && <Spinner className="h-3.5 w-3.5 border-white/40 border-t-white" />}
              {saving ? "保存中" : form.answer_verified ? "确认并加入复习" : "保存为待核验"}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
