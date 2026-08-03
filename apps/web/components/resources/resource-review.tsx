"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";

import { ApiError, apiJson } from "@/lib/api";
import { MathText } from "@/lib/katex";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import type {
  KbOut,
  LearningResourceOut,
  ResourceConfirmOut,
  ResourceQuestionOut,
} from "@/lib/types";

import { SourcePreview } from "./source-preview";
import { ResourceExtractionSteps } from "./resource-extraction-steps";

const QUESTION_TYPES: Array<[string, string]> = [
  ["single", "单选题"],
  ["multi", "多选题"],
  ["blank", "填空题"],
  ["short", "简答题"],
];
const DIFFICULTIES: Array<[string, string]> = [
  ["easy", "简单"],
  ["mid", "中等"],
  ["hard", "困难"],
];
const fieldClass = "mt-1 w-full rounded-[8px] border border-line-strong bg-white px-3 py-2 text-[12px] leading-5 text-ink outline-none transition-colors focus:border-brand-500 disabled:bg-surface-muted disabled:text-ink-muted";

type QuestionDraft = {
  question_no: string;
  page_no: string;
  type: string;
  stem: string;
  options: string[];
  detected_student_answer: string;
  detected_reference_answer: string;
  answer: string;
  explanation: string;
  points: string;
  kc_names: string[];
  difficulty: string;
  extraction_confidence: string;
};

type ConfirmRequest = {
  title: string;
  description: string;
  confirmLabel: string;
  destructive?: boolean;
  onConfirm: () => void;
};

function draftFromQuestion(question: ResourceQuestionOut): QuestionDraft {
  return {
    question_no: question.question_no ?? "",
    page_no: question.page_no?.toString() ?? "",
    type: question.type,
    stem: question.stem,
    options: [...question.options],
    detected_student_answer: question.detected_student_answer ?? "",
    detected_reference_answer: question.detected_reference_answer ?? question.answer ?? "",
    answer: question.answer ?? "",
    explanation: question.explanation ?? "",
    points: question.points?.toString() ?? "",
    kc_names: [...question.kc_names],
    difficulty: question.difficulty || "mid",
    extraction_confidence: question.extraction_confidence == null ? "" : `${Math.round(question.extraction_confidence * 100)}%`,
  };
}

function statusLabel(status: ResourceQuestionOut["verification_status"]): string {
  return status === "verified" ? "已核验" : status === "rejected" ? "已拒绝" : "待校对";
}

function typeLabel(type: string): string {
  return QUESTION_TYPES.find(([value]) => value === type)?.[1] ?? type;
}

function friendlyError(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) {
    const messages: Record<string, string> = {
      RESOURCE_QUESTION_NOT_VERIFIED: "请先核验题目，再进行这项操作。",
      VERIFICATION_ROLLBACK_FORBIDDEN: "题目状态只能向前推进，不能退回。",
      ERROR_ITEM_LOCKED_AFTER_REVIEW: "这道题已经产生学习证据，判分语义字段已锁定。",
      RESOURCE_KB_LOCKED_AFTER_EVIDENCE: "这份资料已有学习证据，不能更换或取消关联知识库。",
      RESOURCE_QUESTION_HAS_ERROR_ITEM: "这道题已经进入错题本，不能删除。",
      RESOURCE_QUESTION_HAS_ASSESSMENT: "这道题已有测评作答记录，不能删除。",
      MODEL_REQUEST_INVALID: "当前模型无法处理该文件，请重试或稍后再试。",
      VISION_ROUTE_UNAVAILABLE: "图片识别服务暂时不可用。",
      PDF_PARTIAL_PAGE_FAILURE: "部分页面未能识别，请检查失败页。",
      NO_QUESTIONS_RECOGNIZED: "没有识别出题目，可以手动添加。",
      RESOURCE_EXTRACTION_FAILED: "资料识别暂时失败，请重试；原文件仍已保留。",
    };
    return messages[reason.code] ?? fallback;
  }
  return fallback;
}

function QuestionStatus({ status }: { status: ResourceQuestionOut["verification_status"] }) {
  const styles = status === "verified"
    ? "bg-success-50 text-success-700"
    : status === "rejected"
      ? "bg-surface-muted text-ink-muted"
      : "bg-warning-50 text-warning-700";
  return <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${styles}`}>{statusLabel(status)}</span>;
}

function QuestionList({
  questions,
  currentId,
  selectedErrors,
  onSelect,
  onToggleError,
}: {
  questions: ResourceQuestionOut[];
  currentId: string | null;
  selectedErrors: Set<string>;
  onSelect: (id: string) => void;
  onToggleError: (id: string, checked: boolean) => void;
}) {
  return (
    <div className="space-y-1.5">
      {questions.map((question, index) => {
        const selected = selectedErrors.has(question.id);
        return (
          <div key={question.id} className={`rounded-[9px] border transition-colors ${currentId === question.id ? "border-ink bg-[#f6f5f2]" : "border-transparent hover:border-line hover:bg-[#fafaf9]"}`}>
            <button type="button" onClick={() => onSelect(question.id)} className="flex w-full items-start gap-2.5 px-2.5 py-2.5 text-left">
              <span className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold ${currentId === question.id ? "bg-ink text-white" : "bg-surface-muted text-ink-muted"}`}>{question.question_no || index + 1}</span>
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-1.5"><span className="text-[10px] text-ink-secondary">{typeLabel(question.type)}</span><QuestionStatus status={question.verification_status} /></span>
                <span className="mt-1 block max-h-9 overflow-hidden text-[11px] leading-4 text-ink">{question.stem || "未填写题干"}</span>
              </span>
            </button>
            {question.verification_status === "verified" && (
              <label className="flex cursor-pointer items-center gap-2 border-t border-line/70 px-2.5 py-2 text-[10px] text-ink-secondary">
                <input type="checkbox" checked={selected} onChange={(event) => onToggleError(question.id, event.target.checked)} className="h-3.5 w-3.5 accent-brand-700" />
                这是我的错题，加入错题本
              </label>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function ResourceReview({
  resource,
  questions,
  onBack,
  onReload,
  onResourceChanged,
  onStartAssessment,
  initialQuestionId = null,
}: {
  resource: LearningResourceOut;
  questions: ResourceQuestionOut[];
  onBack: () => void;
  onReload: () => Promise<void>;
  onResourceChanged: (resource: LearningResourceOut) => void;
  onStartAssessment: () => void;
  initialQuestionId?: string | null;
}) {
  const [currentId, setCurrentId] = useState<string | null>(() => {
    if (initialQuestionId && questions.some((question) => question.id === initialQuestionId)) {
      return initialQuestionId;
    }
    return questions[0]?.id ?? null;
  });
  const [draft, setDraft] = useState<QuestionDraft | null>(() => {
    const initial = (initialQuestionId && questions.find((question) => question.id === initialQuestionId)) ?? questions[0];
    return initial ? draftFromQuestion(initial) : null;
  });
  const [dirty, setDirty] = useState(false);
  const [selectedErrors, setSelectedErrors] = useState<Set<string>>(
    () => new Set(questions.filter((question) => question.is_error_selected).map((question) => question.id)),
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [mobilePane, setMobilePane] = useState<"source" | "questions">("questions");
  const [showManual, setShowManual] = useState(false);
  const [manualStem, setManualStem] = useState("");
  const [manualType, setManualType] = useState("short");
  const [knowledgeBases, setKnowledgeBases] = useState<KbOut[]>([]);
  const [linkedKbId, setLinkedKbId] = useState(resource.linked_kb_id ?? "");
  const [kbBusy, setKbBusy] = useState(false);
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null);

  const current = questions.find((question) => question.id === currentId) ?? null;
  const semanticLocked = current?.content_editable === false;
  const answerHidden = current != null && current.answer === undefined;
  const pendingCount = questions.filter((question) => question.verification_status === "pending").length;
  const verifiedCount = questions.filter((question) => question.verification_status === "verified").length;
  const selectedCount = useMemo(() => selectedErrors.size, [selectedErrors]);
  const failedPages = Array.isArray(resource.metadata.failed_pages)
    ? resource.metadata.failed_pages
    : [];

  useEffect(() => {
    if (currentId && questions.some((question) => question.id === currentId)) return;
    const initialQuestion = initialQuestionId
      ? questions.find((question) => question.id === initialQuestionId)
      : undefined;
    const next = initialQuestion ?? questions[0];
    const timer = window.setTimeout(() => {
      setCurrentId(next?.id ?? null);
      setDraft(next ? draftFromQuestion(next) : null);
      setDirty(false);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [currentId, initialQuestionId, questions]);

  const retryExtraction = async () => {
    if (!resource.source_url) return;
    setBusy("extract");
    setMessage(null);
    try {
      const updated = await apiJson<LearningResourceOut>(`/api/resources/${resource.id}/extract`, {
        method: "POST",
        body: {},
      });
      onResourceChanged({ ...resource, ...updated });
      setMessage("已重新提交识别，原文件仍会保留。");
      await onReload();
    } catch (reason) {
      setMessage(friendlyError(reason, "识别任务提交失败，请稍后重试"));
    } finally {
      setBusy(null);
    }
  };

  useEffect(() => {
    let active = true;
    void apiJson<KbOut[]>("/api/kb")
      .then((items) => {
        if (active) setKnowledgeBases(items);
      })
      .catch(() => {
        // Knowledge-point linking is optional and should not block reviewing.
      });
    return () => {
      active = false;
    };
  }, []);

  const updateDraft = <K extends keyof QuestionDraft>(key: K, value: QuestionDraft[K]) => {
    setDraft((previous) => previous ? { ...previous, [key]: value } : previous);
    setDirty(true);
  };

  const switchQuestion = (id: string) => {
    const question = questions.find((item) => item.id === id);
    if (!question) return;
    setCurrentId(id);
    setDraft(draftFromQuestion(question));
    setDirty(false);
    setMessage(null);
  };

  const selectQuestion = (id: string) => {
    if (id === currentId) return;
    if (dirty) {
      setConfirmRequest({
        title: "放弃未保存修改？",
        description: "当前题目还有未保存的校对内容，切换后这些修改会丢失。",
        confirmLabel: "放弃并切换",
        destructive: true,
        onConfirm: () => {
          setConfirmRequest(null);
          switchQuestion(id);
        },
      });
      return;
    }
    switchQuestion(id);
  };

  const saveQuestion = async (question = current): Promise<ResourceQuestionOut | null> => {
    if (!question || !draft) return null;
    setBusy("save");
    setMessage(null);
    const payload: Record<string, unknown> = {
      question_no: draft.question_no.trim() || null,
      page_no: draft.page_no ? Number(draft.page_no) : null,
      stem: draft.stem.trim(),
      detected_student_answer: draft.detected_student_answer.trim(),
      explanation: draft.explanation.trim(),
      points: draft.points ? Number(draft.points) : null,
      difficulty: draft.difficulty,
    };
    if (!semanticLocked && !answerHidden) {
      payload.type = draft.type;
      payload.options = draft.options.map((item) => item.trim()).filter(Boolean);
      payload.detected_reference_answer = draft.detected_reference_answer.trim();
      payload.answer = draft.answer.trim();
      payload.kc_names = draft.kc_names.map((item) => item.trim()).filter(Boolean).slice(0, 3);
    }
    try {
      if (!payload.stem) throw new Error("请先填写题干");
      const saved = await apiJson<ResourceQuestionOut>(`/api/resource-questions/${question.id}`, {
        method: "PATCH",
        body: payload,
      });
      setDraft(draftFromQuestion(saved));
      setDirty(false);
      await onReload();
      setMessage("题目修改已保存。");
      return saved;
    } catch (reason) {
      setMessage(friendlyError(reason, "题目保存失败，请稍后重试"));
      return null;
    } finally {
      setBusy(null);
    }
  };

  const verifyQuestion = async () => {
    if (!current || !draft) return;
    if (dirty && !(await saveQuestion())) return;
    setBusy("verify");
    setMessage(null);
    try {
      const saved = await apiJson<ResourceQuestionOut>(`/api/resource-questions/${current.id}/verify`, {
        method: "POST",
        body: {},
      });
      setDraft(draftFromQuestion(saved));
      setDirty(false);
      await onReload();
      setMessage("题目已核验。是否加入错题本，请在左侧明确勾选。 ");
    } catch (reason) {
      setMessage(friendlyError(reason, "核验失败，请先确认题目内容完整"));
    } finally {
      setBusy(null);
    }
  };

  const rejectQuestion = async () => {
    if (!current || current.verification_status !== "pending") return;
    setBusy("reject");
    setMessage(null);
    try {
      await apiJson<ResourceQuestionOut>(`/api/resource-questions/${current.id}`, {
        method: "PATCH",
        body: { verification_status: "rejected" },
      });
      setSelectedErrors((previous) => {
        const next = new Set(previous);
        next.delete(current.id);
        return next;
      });
      await onReload();
      setMessage("题目已拒绝，不会进入错题本或测评。");
    } catch (reason) {
      setMessage(friendlyError(reason, "拒绝题目失败"));
    } finally {
      setBusy(null);
    }
  };

  const requestRejectQuestion = () => {
    if (!current || current.verification_status !== "pending") return;
    setConfirmRequest({
      title: "拒绝这道识别题？",
      description: "拒绝后这道题不会进入错题本或真实作答记录。",
      confirmLabel: "拒绝题目",
      destructive: true,
      onConfirm: () => {
        setConfirmRequest(null);
        void rejectQuestion();
      },
    });
  };

  const applyLinkedKb = async (value: string) => {
    setLinkedKbId(value);
    setKbBusy(true);
    setMessage(null);
    try {
      const updated = await apiJson<LearningResourceOut>(`/api/resources/${resource.id}`, {
        method: "PATCH",
        body: { linked_kb_id: value || null },
      });
      onResourceChanged({
        ...resource,
        ...updated,
        question_count: resource.question_count,
        pending_question_count: resource.pending_question_count,
      });
      await onReload();
      setMessage(value ? "已更新关联知识库。" : "已取消关联知识库。 ");
    } catch (reason) {
      setLinkedKbId(resource.linked_kb_id ?? "");
      setMessage(friendlyError(reason, "更新知识库关联失败"));
    } finally {
      setKbBusy(false);
    }
  };

  const updateLinkedKb = (value: string) => {
    if (value === linkedKbId || kbBusy) return;
    setConfirmRequest({
      title: value ? "更换关联知识库？" : "取消关联知识库？",
      description: value
        ? "尚未产生学习证据的题目会按已保存的知识点名称重新映射到新知识库；已有学习证据的资料将由服务端拒绝修改。"
        : "取消关联后，尚未产生学习证据的题目会清空知识点 ID，避免继续引用旧知识库。",
      confirmLabel: value ? "更换并重映射" : "取消关联",
      onConfirm: () => {
        setConfirmRequest(null);
        void applyLinkedKb(value);
      },
    });
  };

  const confirmErrors = async () => {
    if (dirty && !(await saveQuestion())) return;
    setBusy("confirm");
    setMessage(null);
    try {
      const result = await apiJson<ResourceConfirmOut>(`/api/resources/${resource.id}/confirm`, {
        body: { error_question_ids: [...selectedErrors], add_to_error_book: true },
      });
      onResourceChanged({ ...resource, status: result.status, pending_question_count: pendingCount });
      await onReload();
      setMessage(selectedCount > 0 ? `已将 ${selectedCount} 道明确选择的题加入错题本。` : "校对进度已保存；本次没有选择任何错题。 ");
    } catch (reason) {
      setMessage(friendlyError(reason, "保存校对结果失败"));
    } finally {
      setBusy(null);
    }
  };

  const addManualQuestion = async (event: FormEvent) => {
    event.preventDefault();
    if (!manualStem.trim()) return;
    setBusy("manual");
    setMessage(null);
    try {
      await apiJson<ResourceQuestionOut>(`/api/resources/${resource.id}/questions/manual`, {
        body: { stem: manualStem.trim(), type: manualType, answer: "" },
      });
      setManualStem("");
      setShowManual(false);
      await onReload();
      setMessage("已添加一题待校对题目。");
    } catch (reason) {
      setMessage(friendlyError(reason, "添加题目失败"));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mx-auto w-full max-w-[1480px] space-y-4 px-5 py-5 sm:px-8 lg:px-12 lg:py-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <button type="button" onClick={onBack} className="text-[11px] text-ink-muted hover:text-ink">← 返回题目资料</button>
          <h1 className="mt-2 truncate text-[22px] font-semibold tracking-[-0.03em] text-ink">{resource.title}</h1>
          <p className="mt-1 text-[11px] text-ink-secondary">{resource.subject || "通用"} · {verifiedCount} 道已核验 · {pendingCount} 道待校对 · {resource.source_filename ?? "手动创建"}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select value={linkedKbId} onChange={(event) => void updateLinkedKb(event.target.value)} disabled={kbBusy} aria-label="关联知识库" className="h-8 max-w-[220px] rounded-[7px] border border-line-strong bg-white px-2 text-[10px] text-ink-secondary disabled:opacity-50"><option value="">不关联知识库</option>{knowledgeBases.map((kb) => <option key={kb.id} value={kb.id}>{kb.name}</option>)}</select>
          <button type="button" onClick={onStartAssessment} disabled={verifiedCount === 0} className="rounded-[8px] border border-line-strong bg-white px-3.5 py-2 text-[11px] font-medium text-ink-secondary hover:border-ink-muted hover:text-ink disabled:cursor-not-allowed disabled:opacity-40">作答与错题记录</button>
          <button type="button" onClick={() => void confirmErrors()} disabled={busy !== null || questions.length === 0} className="rounded-[8px] bg-ink px-3.5 py-2 text-[11px] font-medium text-white disabled:opacity-40">{busy === "confirm" ? "正在保存…" : selectedCount > 0 ? `确认并加入 ${selectedCount} 道错题` : "保存校对进度"}</button>
        </div>
      </header>

      <ResourceExtractionSteps resource={resource} />

      {message && <div className="rounded-[9px] border border-line bg-white px-3.5 py-2.5 text-[11px] text-ink-secondary">{message}</div>}
      {!linkedKbId && <div className="rounded-[9px] border border-warning-200 bg-warning-50 px-3.5 py-2.5 text-[11px] leading-5 text-warning-700">未关联知识库时，错题仍可进入 FSRS；知识点掌握度和章节画像可能无法完整归类。</div>}

      {(resource.status === "failed" || failedPages.length > 0 || questions.length === 0) && (
        <div className={`rounded-[10px] border px-4 py-3 text-[11px] leading-5 ${resource.status === "failed" ? "border-danger-200 bg-danger-50 text-danger-700" : "border-warning-200 bg-warning-50 text-warning-700"}`}>
          <p className="font-medium">{resource.status === "failed" ? "识别未完成，原文件已经保留。" : failedPages.length > 0 ? "部分页面未能识别，已保留成功题目。" : "没有识别出可校对题目。"}</p>
          {failedPages.length > 0 && <p className="mt-1">失败页：{failedPages.join("、")}</p>}
          <div className="mt-2 flex flex-wrap gap-2">
            {resource.source_url && <button type="button" onClick={() => void retryExtraction()} disabled={busy !== null} className="border border-current bg-white px-2.5 py-1.5 disabled:opacity-40">{busy === "extract" ? "提交中…" : "重新识别"}</button>}
            <button type="button" onClick={() => setShowManual(true)} className="border border-current bg-white px-2.5 py-1.5">手动添加题目</button>
            {resource.source_url && <a href={resource.source_url} target="_blank" rel="noreferrer" className="border border-current bg-white px-2.5 py-1.5">查看原文件</a>}
          </div>
        </div>
      )}

      <div className="flex w-fit rounded-[8px] border border-line bg-white p-1 lg:hidden">
        <button type="button" onClick={() => setMobilePane("source")} className={`rounded-[6px] px-3 py-1.5 text-[11px] font-medium ${mobilePane === "source" ? "bg-ink text-white" : "text-ink-secondary"}`}>原件预览</button>
        <button type="button" onClick={() => setMobilePane("questions")} className={`rounded-[6px] px-3 py-1.5 text-[11px] font-medium ${mobilePane === "questions" ? "bg-ink text-white" : "text-ink-secondary"}`}>题目校对</button>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(280px,0.8fr)_minmax(0,1.4fr)]">
        <div className={mobilePane === "source" ? "block" : "hidden lg:block"}><SourcePreview resource={resource} /></div>
        <section className={mobilePane === "questions" ? "space-y-4" : "hidden lg:block lg:space-y-4"}>
          <div className="grid gap-4 lg:grid-cols-[220px_minmax(0,1fr)]">
            <aside className="rounded-[12px] border border-line bg-white p-3">
              <div className="flex items-center justify-between gap-2 border-b border-line pb-3">
                <div><h2 className="text-[12px] font-semibold text-ink">题目列表</h2><p className="mt-0.5 text-[10px] text-ink-muted">显式选择错题</p></div>
                <button type="button" onClick={() => setShowManual((value) => !value)} className="rounded-[7px] border border-line-strong px-2 py-1 text-[10px] text-ink-secondary hover:text-ink">+ 添加题目</button>
              </div>
              <div className="mt-3 max-h-[62dvh] overflow-y-auto"><QuestionList questions={questions} currentId={currentId} selectedErrors={selectedErrors} onSelect={selectQuestion} onToggleError={(id, checked) => setSelectedErrors((previous) => { const next = new Set(previous); if (checked) next.add(id); else next.delete(id); return next; })} /></div>
              {showManual && (
                <form onSubmit={addManualQuestion} className="mt-3 space-y-2 border-t border-line pt-3">
                  <textarea value={manualStem} onChange={(event) => setManualStem(event.target.value)} rows={3} placeholder="输入新题干" className={`${fieldClass} resize-y`} />
                  <select value={manualType} onChange={(event) => setManualType(event.target.value)} className={fieldClass}><option value="short">简答题</option><option value="single">单选题</option><option value="multi">多选题</option><option value="blank">填空题</option></select>
                  <button type="submit" disabled={busy === "manual" || !manualStem.trim()} className="w-full rounded-[7px] bg-ink px-3 py-2 text-[10px] font-medium text-white disabled:opacity-40">添加待校对题</button>
                </form>
              )}
            </aside>

            <div className="min-w-0 rounded-[12px] border border-line bg-white p-4 sm:p-5">
              {!current || !draft ? (
                <div className="flex min-h-64 items-center justify-center text-[12px] text-ink-muted">暂时没有题目。可以从左侧添加手动题目。</div>
              ) : (
                <>
                  <div className="flex flex-wrap items-start justify-between gap-2 border-b border-line pb-3">
                    <div><div className="flex flex-wrap items-center gap-2"><span className="text-[12px] font-semibold text-ink">第 {current.question_no || current.ordinal + 1} 题</span><QuestionStatus status={current.verification_status} /><span className="text-[10px] text-ink-muted">{typeLabel(current.type)}</span></div><p className="mt-1 text-[10px] text-ink-muted">识别置信度：{draft.extraction_confidence || "未提供"}</p></div>
                    <div className="flex flex-wrap gap-2">
                      <button type="button" onClick={() => void saveQuestion()} disabled={busy !== null || !dirty} className="rounded-[7px] border border-line-strong px-3 py-1.5 text-[10px] font-medium text-ink-secondary disabled:opacity-40">{busy === "save" ? "保存中…" : "保存修改"}</button>
                      {current.verification_status === "pending" && <><button type="button" onClick={requestRejectQuestion} disabled={busy !== null} className="rounded-[7px] border border-danger-200 px-3 py-1.5 text-[10px] text-danger-700 disabled:opacity-40">拒绝</button><button type="button" onClick={() => void verifyQuestion()} disabled={busy !== null || !draft.stem.trim() || !draft.answer.trim()} className="rounded-[7px] bg-ink px-3 py-1.5 text-[10px] font-medium text-white disabled:opacity-40">{busy === "verify" ? "核验中…" : "核验题目"}</button></>}
                    </div>
                  </div>

                  <div className="mt-4 grid gap-4 sm:grid-cols-3">
                    <label><span className="text-[10px] font-medium text-ink-secondary">题号</span><input value={draft.question_no} onChange={(event) => updateDraft("question_no", event.target.value)} className={fieldClass} /></label>
                    <label><span className="text-[10px] font-medium text-ink-secondary">页码</span><input type="number" min={1} value={draft.page_no} onChange={(event) => updateDraft("page_no", event.target.value)} className={fieldClass} /></label>
                    <label><span className="text-[10px] font-medium text-ink-secondary">题型</span><select disabled={semanticLocked || answerHidden} value={draft.type} onChange={(event) => updateDraft("type", event.target.value)} className={fieldClass}>{QUESTION_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
                  </div>
                  <label className="mt-4 block"><span className="text-[10px] font-medium text-ink-secondary">题干 <em className="not-italic text-danger-600">*</em></span><textarea rows={5} value={draft.stem} onChange={(event) => updateDraft("stem", event.target.value)} className={`${fieldClass} resize-y`} /></label>
                  {(draft.type === "single" || draft.type === "multi") && <label className="mt-4 block"><span className="text-[10px] font-medium text-ink-secondary">选项（每行一个）</span><textarea disabled={semanticLocked || answerHidden} rows={4} value={draft.options.join("\n")} onChange={(event) => updateDraft("options", event.target.value.split("\n"))} className={`${fieldClass} resize-y font-mono`} /></label>}
                  <div className="mt-4 grid gap-4 sm:grid-cols-2"><label><span className="text-[10px] font-medium text-ink-secondary">学生原答案</span><textarea rows={3} value={draft.detected_student_answer} onChange={(event) => updateDraft("detected_student_answer", event.target.value)} className={`${fieldClass} resize-y`} /></label><label><span className="text-[10px] font-medium text-ink-secondary">参考答案</span><textarea disabled={semanticLocked || answerHidden} rows={3} value={draft.detected_reference_answer} onChange={(event) => { updateDraft("detected_reference_answer", event.target.value); updateDraft("answer", event.target.value); }} className={`${fieldClass} resize-y`} /></label></div>
                  <div className="mt-4 grid gap-4 sm:grid-cols-[minmax(0,1fr)_140px_160px]"><label><span className="text-[10px] font-medium text-ink-secondary">知识点（最多 3 个）</span><input disabled={semanticLocked || answerHidden} value={draft.kc_names.join("，")} onChange={(event) => updateDraft("kc_names", event.target.value.split(/[,，]/))} placeholder="例如：二次函数，最值" className={fieldClass} /></label><label><span className="text-[10px] font-medium text-ink-secondary">分值</span><input type="number" min={0} step="0.5" value={draft.points} onChange={(event) => updateDraft("points", event.target.value)} className={fieldClass} /></label><label><span className="text-[10px] font-medium text-ink-secondary">难度</span><select value={draft.difficulty} onChange={(event) => updateDraft("difficulty", event.target.value)} className={fieldClass}>{DIFFICULTIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div>
                  <label className="mt-4 block"><span className="text-[10px] font-medium text-ink-secondary">解析</span><textarea rows={4} value={draft.explanation} onChange={(event) => updateDraft("explanation", event.target.value)} className={`${fieldClass} resize-y`} /></label>
                  <div className="mt-4 rounded-[9px] bg-surface-muted px-3 py-2.5 text-[11px] leading-5 text-ink-secondary"><span className="font-medium text-ink">核验规则：</span>只有点击“核验题目”后，题目才能进入真实作答；是否属于错题必须单独勾选。待校对和已拒绝题不会影响学习数据。{semanticLocked && <span className="mt-1 block text-warning-700">这道题已经产生 FSRS 或掌握度证据，题型、选项、参考答案和知识点已锁定。</span>}{answerHidden && <span className="mt-1 block text-warning-700">当前存在未完成作答记录，参考答案暂时隐藏；请先完成或关闭该草稿记录。</span>}</div>
                  {current.verification_status === "verified" && <div className={`mt-3 flex items-center gap-2 rounded-[8px] px-3 py-2 text-[10px] ${selectedErrors.has(current.id) ? "bg-warning-50 text-warning-700" : "bg-success-50 text-success-700"}`}><input type="checkbox" checked={selectedErrors.has(current.id)} onChange={(event) => setSelectedErrors((previous) => { const next = new Set(previous); if (event.target.checked) next.add(current.id); else next.delete(current.id); return next; })} className="h-3.5 w-3.5 accent-brand-700" />{selectedErrors.has(current.id) ? "已选择：这是一道错题" : "这道题暂不加入错题本"}</div>}
                  <div className="mt-4 rounded-[9px] border border-line bg-[#fafaf9] p-3"><p className="text-[10px] font-semibold text-ink-muted">题干预览</p><div className="mt-2 text-[12px] leading-6 text-ink"><MathText text={draft.stem || "（尚未填写题干）"} /></div></div>
                </>
              )}
            </div>
          </div>
        </section>
      </div>
      <ConfirmDialog
        open={confirmRequest !== null}
        title={confirmRequest?.title ?? "请确认"}
        description={confirmRequest?.description ?? ""}
        confirmLabel={confirmRequest?.confirmLabel}
        destructive={confirmRequest?.destructive}
        onCancel={() => setConfirmRequest(null)}
        onConfirm={() => confirmRequest?.onConfirm()}
      />
    </div>
  );
}
